"""Antivirus and malware scanning module for My-IDM."""

from __future__ import annotations

import json
import logging
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)

# Stand-in for the `%file%` / `%f` placeholder while the argument template is split. Substituted
# with the real path *after* splitting, so a path containing a space stays a single argument.
# Printable on purpose: Windows rejects a NUL anywhere in an argument string, and although this
# text never reaches the OS, keeping it printable removes any need to reason about that.
_TEMPLATE_SENTINEL = "MYIDM_SCAN_TARGET_a7f3c1e9"

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


def running_on_windows() -> bool:
    """Whether this process is running on Windows.

    Deliberately behind a function rather than read inline at each branch. The Defender verdict
    logic is worth testing on Linux and macOS - it is the fail-closed behaviour that decides
    whether a downloaded file is reported clean - and steering it there means faking the platform.
    Patching ``sys.platform`` to ``"win32"`` fakes it for the entire interpreter, standard library
    included: ``shutil.which`` then takes its own Windows branch and dereferences ``_winapi``,
    which is ``None`` off Windows. That turned the ClamAV lookup into an ``AttributeError`` rather
    than a clean "not installed". Tests patch this one function instead.
    """
    return sys.platform == "win32"


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
                    # `ntpath`, not `os.path`: this function only ever handles Windows paths,
                    # and on a POSIX host `os.path.dirname` finds no separator in
                    # `C:\...\Platform\4.20.2\MpCmdRun.exe`, so every candidate would score as
                    # version (0,) and the sort would silently degenerate to enumeration order.
                    # On Windows `ntpath is os.path`, so nothing changes there.
                    full_path = ntpath.join(root, "MpCmdRun.exe")
                    if os.path.isfile(full_path):
                        found.append(full_path)
            if found:
                def _version(path: str):
                    # ...\Platform\<version>\MpCmdRun.exe
                    name = ntpath.basename(ntpath.dirname(path))
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


#: Executables tried, in order, when auto-detecting a POSIX scanner. `clamdscan` is listed first
#: because it talks to a running `clamd` and is much faster on large files, but it needs that
#: daemon to be up - so a missing daemon makes it fail where `clamscan` would have worked.
#: Order is therefore the lesser-evil choice, and both are reported the same way if neither works.
_CLAMSCAN_EXECUTABLES = ("clamdscan", "clamscan")

#: Default argument template for an auto-detected scanner.
#: `--no-summary` because My-IDM builds its own report line from the exit code, and clamscan's
#: per-file scan summary would only be noise. clamscan exits 0 clean, 1 infected, 2 error.
_CLAMSCAN_ARGS = "--no-summary %file%"


def find_clamav() -> tuple[Optional[str], Optional[str]]:
    """Locate a ClamAV command-line scanner. Returns ``(executable, args_template)``.

    ClamAV is the only practical antivirus on Linux and the only installable one on macOS
    (``brew install clamav``). Neither platform ships a Defender equivalent, so without this the
    post-download scan has nothing to run and reports "not scanned" - correct, but only useful if
    the user knows ClamAV is the answer.

    Detection only, never installation. Installing a security tool, let alone a system daemon, on
    a user's machine without being asked is not this application's decision to make; a packaged
    build will simply find its own bundled copy through this same lookup.
    """
    for name in _CLAMSCAN_EXECUTABLES:
        found = shutil.which(name)
        if found:
            return found, _CLAMSCAN_ARGS
    return None, None


def _scan_with_custom(
    scanner: str,
    args_template: Optional[str],
    abs_path: str,
    config: SecurityConfig,
) -> tuple[bool | None, str]:
    """Run an arbitrary command-line scanner and interpret its exit code.

    Shared by the configured-custom branch and by ClamAV auto-detection, which is why the
    executable and the argument template are parameters rather than read from *config*.

    Builds an argv list, never a shell string. This used to interpolate the target path into a
    ``shell=True`` command line: a download whose filename contains a double quote breaks out of
    the surrounding quotes, and ``&``, ``|`` or ``$(...)`` in a path then execute - so a crafted
    torrent could run arbitrary commands as the user. The path is attacker-influenced, which is
    what makes that an injection rather than a quoting nit.

    ``shlex.split`` gives the template the same treatment a shell would, minus the shell. The
    placeholder is swapped for a sentinel first so the path is never split on: a path containing a
    space must stay a single argument.
    """
    if not os.path.isfile(scanner):
        return None, f"Configured custom antivirus executable not found: {scanner}"

    template = args_template or '"%file%"'
    template = template.replace("%file%", _TEMPLATE_SENTINEL).replace("%f", _TEMPLATE_SENTINEL)
    argv = shlex.split(template, posix=os.name != "nt")
    # With posix=False, shlex keeps the surrounding quote characters, so strip them before
    # comparing against the sentinel.
    argv = [abs_path if arg.strip('"') == _TEMPLATE_SENTINEL else arg for arg in argv]
    cmd = [scanner, *argv]
    log.info("Running antivirus scan: %s", cmd)

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        # 0 is clean, non-zero is a threat *or* an error - clamscan's 1 (infected) and 2 (error)
        # are indistinguishable here, and both are "not clean". The exclusion list still applies,
        # so a user who has excluded a category keeps that behaviour on ClamAV too.
        if res.returncode == 0:
            return True, f"Clean (Custom Scanner: {Path(scanner).name})"
        out = (res.stdout + "\n" + res.stderr).strip()
        threat_report = (
            f"Threat detected or scanner alert (Exit code {res.returncode}): {out[:200]}"
        )
        excluded = _is_threat_excluded(threat_report, config)
        if excluded:
            return True, f"Allowed (matched exclusion '{excluded}'): {threat_report}"
        return False, threat_report
    except subprocess.TimeoutExpired:
        return None, "Custom scan timed out after 60 seconds."
    except Exception as exc:
        log.error("Custom scan failed: %s", exc)
        return None, f"Custom scan could not be executed: {exc}"


def _scan_with_defender(
    defender: str, abs_path: str, config: SecurityConfig
) -> tuple[bool | None, str]:
    """Run Windows Defender's command-line scanner and interpret its exit code."""
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
        if res.returncode == 2:
            threat_report = f"⚠️ Threat detected by Windows Defender!\n{out}"
            excluded = _is_threat_excluded(threat_report, config)
            if excluded:
                return True, f"Allowed (matched exclusion '{excluded}'): {threat_report}"
            return False, threat_report
        if "found no threats" in out.lower():
            return True, "Clean (Windows Defender: no threats found)"
        if "threat" in out.lower():
            threat_report = f"⚠️ Threat detected by Windows Defender: {out}"
            excluded = _is_threat_excluded(threat_report, config)
            if excluded:
                return True, f"Allowed (matched exclusion '{excluded}'): {threat_report}"
            return False, threat_report
        # An unrecognised exit code with output matching neither verdict is not a clean result,
        # it is an unread one. Same reasoning as a missing scanner.
        return None, (
            f"Windows Defender exited with code {res.returncode} and produced no "
            f"recognisable verdict; the file was not scanned."
        )
    except subprocess.TimeoutExpired:
        log.warning("Windows Defender scan timed out on %s", abs_path)
        return None, "Windows Defender scan timed out; the file was not scanned."
    except Exception as exc:
        log.error("Windows Defender execution failed: %s", exc)
        return None, f"Windows Defender could not be executed: {exc}"


def scan_file(file_path: str, config: SecurityConfig) -> tuple[bool | None, str]:
    """Scan a downloaded file with the configured antivirus scanner.

    Returns ``(verdict, report_message)`` where **verdict is tri-state**:

    * ``True``  - the scanner ran and found nothing.
    * ``False`` - the scanner found a threat.
    * ``None``  - **no verdict could be reached**: no scanner is installed, the scanner is
      misconfigured, it timed out, or it could not be executed.

    ``None`` used to be reported as ``True``. That is a fail-*open* defect, not a lenient default:
    on any machine without Windows Defender - which is every Linux and macOS machine - every scan
    returned a clean verdict for a file nothing had looked at, and the UI showed a green
    "Clean (Scanned)". A scanner that could not run says nothing about the file, and rendering that
    as safe is the one answer that cannot be allowed.

    Threats matching excluded categories/patterns are treated as clean.
    """
    if not config.scan_after_download:
        return True, "Post-download scan is disabled."

    file_p = Path(file_path)
    if not file_p.exists():
        return True, f"File does not exist on disk: {file_path}"

    abs_path = str(file_p.resolve())

    scanner_type = (config.scanner_type or "auto").strip().lower()

    if scanner_type == "custom" and config.custom_scanner_path:
        return _scan_with_custom(
            config.custom_scanner_path, config.custom_scanner_args, abs_path, config
        )

    # `custom` with an empty Executable field is a misconfiguration, not a choice of scanner.
    # Falling back to `auto` keeps a blank field from silently disabling scanning - the user did
    # not ask for no scanning, they left a text box empty.
    if scanner_type == "custom":
        scanner_type = "auto"

    # "defender" is the historical value of the Windows/Custom radio button, so it is also what
    # every existing configuration has stored - including on Linux and macOS, where Defender
    # cannot exist. Treating it there as "the system scanner" is what makes the default settings
    # work off Windows at all; the radio is relabelled per platform so the UI says so.
    if scanner_type == "defender" and not running_on_windows():
        log.info(
            "Scanner is set to 'defender' but this is %s; using the system scanner instead. "
            "The Settings label reads 'System scanner' on this platform.",
            sys.platform,
        )
        scanner_type = "auto"

    # `auto`: use Defender where it exists, otherwise fall back to whatever ClamAV is installed.
    # This is what makes the *default* configuration meaningful on Linux and macOS, rather than
    # every scan reporting "not scanned" because no antivirus happens to be installed.
    if scanner_type == "auto":
        if running_on_windows():
            defender = find_windows_defender_path()
            if defender:
                return _scan_with_defender(defender, abs_path, config)
            clam, clam_args = find_clamav()
            if clam:
                return _scan_with_custom(clam, clam_args, abs_path, config)
            log.warning(
                "No scanner available - Windows Defender and ClamAV are both absent. "
                "The file was NOT scanned."
            )
            return None, (
                "No antivirus scanner available (Windows Defender not found and "
                "ClamAV is not installed). Install ClamAV, or set a Custom Antivirus "
                "Scanner in Settings; the file was not scanned."
            )
        clam, clam_args = find_clamav()
        if clam:
            return _scan_with_custom(clam, clam_args, abs_path, config)
        log.warning("Neither clamscan nor clamdscan found on PATH - the file was NOT scanned")
        return None, (
            "No antivirus scanner available. Install ClamAV (apt install clamav / "
            "brew install clamav), or set a Custom Antivirus Scanner in Settings; "
            "the file was not scanned."
        )

    if scanner_type == "defender":
        defender = find_windows_defender_path()
        if not defender:
            log.warning(
                "Windows Defender (MpCmdRun.exe) not found on this system - "
                "the file was NOT scanned"
            )
            return None, (
                "No antivirus scanner available (Windows Defender not found). "
                "The file was not scanned."
            )
        return _scan_with_defender(defender, abs_path, config)

    # `clamscan` / `clamav`: an explicit POSIX selection, honouring configured arguments.
    clam, clam_args = find_clamav()
    if not clam:
        return None, (
            "ClamAV is selected but neither clamscan nor clamdscan is on PATH. "
            "The file was not scanned."
        )
    return _scan_with_custom(clam, config.custom_scanner_args or clam_args, abs_path, config)


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
