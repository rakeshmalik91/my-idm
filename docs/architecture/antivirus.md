# Feature Guide: Antivirus & Malware Protection

## Overview

My-IDM incorporates multi-layered antivirus and security protections to safeguard your system against malware, phishing, ransomware, and deceptive payloads. Security checks occur both **before a download starts** (inspecting URLs, file extensions, and reputation) and **immediately after a download completes** (running on-disk antivirus scanning via Windows Defender or a custom antivirus engine).

In addition, users can initiate on-demand scans at any time for any completed file directly from the download list.

---

## Key Capabilities

### 1. Pre-Download Threat Inspection & Heuristics
Before initiating network connections or allocating local disk space, My-IDM inspects the target download URL for suspicious patterns:
- **Deceptive Double Extensions**: Detects masquerading filenames such as `document.pdf.exe`, `invoice.xlsx.scr`, or `photo.jpg.bat`—a prevalent technique used by malware to trick users into executing code.
- **High-Risk Executable Warnings**: Flags direct script and binary extensions (`.exe`, `.scr`, `.bat`, `.cmd`, `.vbs`, `.vbe`, `.js`, `.jse`, `.wsf`, `.wsh`, `.msc`, `.msi`, `.msp`, `.pif`, `.hta`, `.cpl`, `.jar`, `.gadget`, `.iso`, `.img`, `.ps1`).
- **Bare IP Address Warnings**: Flags downloads served directly from raw IP addresses (e.g. `http://198.51.100.24/file.zip`) rather than verified domain names, frequently used in ephemeral drive-by download campaigns.
- **VirusTotal API Integration (Optional)**: If you provide a free VirusTotal API key, My-IDM queries over 70 commercial antivirus engines and threat intelligence databases before downloading.
- **Configurable Action**: Choose whether suspicious URLs should only trigger an alert warning or be blocked outright.

### 2. Post-Download Antivirus File Scanning
Once all segments or torrent pieces are successfully assembled on disk, My-IDM automatically triggers a silent background scan:
- **Built-in Windows Defender Integration (`MpCmdRun.exe`)**:
  - Automatically discovers the Microsoft Defender Command-Line Utility across standard install locations and dynamic `ProgramData\Microsoft\Windows Defender\Platform` engine update directories.
  - Executes dedicated single-file scans (`-Scan -ScanType 3 -File "<path>" -DisableRemediation`).
  - Accurately interprets return codes: code `0` (Clean) and code `2` (Threat Detected).
- **Custom Antivirus Scanner Support**:
  - Integrate any third-party antivirus solution (e.g., ClamAV `clamscan.exe`, Malwarebytes, ESET, Kaspersky, or Sophos).
  - Flexible command-line template supporting `%file%` or `%f` replacement tokens for the absolute file path (e.g. `"C:\Program Files\ClamAV\clamscan.exe" --bell -i "%file%"`).
  - Evaluates process exit codes to determine file safety.

### 3. Threat Remediation Actions
When an infected or malicious file is detected:
- **Warn (Default)**: Displays an urgent alert dialog identifying the threat name, scanner engine output, and file path, while tagging the download in the UI.
- **Delete / Quarantine**: Automatically removes the infected file or directory from disk immediately (`quarantine_or_delete_file`), preventing accidental execution or background OS indexing.

### 4. Threat Exclusions List
To prevent false-positive alerts on commonly encountered torrent payloads (keygens, cracks, game patches, or diagnostic utilities), My-IDM allows configuring a list of excluded threat patterns:
- Managed as an interactive list widget with **Add**, **Remove**, and **↺ Reset Defaults** controls.
- By default, common harmless categories can be pre-filled: `HackTool`, `CrackTool`, `PUA`, `Adware`, and `Riskware`.
- Any scanner threat output matching an exclusion item is silently allowed as clean (`Allowed (matched exclusion '...')`).

### 5. Scan Timing Control
Users can specify when antivirus scanning occurs:
- **🔄 Automatically scan when download completes (Default)**: Immediately runs the scanner upon completion of HTTP or torrent downloads.
- **🖱️ Manual scan only**: Completely disables automatic post-download scanning, allowing users to scan on-demand via right-click whenever desired.

### 6. On-Demand Right-Click Scanning
- Right-click any completed download in the transfer table and select **🛡️ Scan with Antivirus**.
- The scan runs asynchronously in a worker thread without freezing the user interface.
- Results update the download metadata and are immediately viewable in the Details Panel.

### 7. Details Panel Security Reporting
- The bottom Details Panel displays security audit information in the **Overview** tab:
  - **🛡️ Antivirus: Clean** (green) with scanner verification notes.
  - **⚠️ Antivirus: Threat Detected** (red) with complete engine logs.

---

## User Interface & Controls

### Opening Security Settings
- **Menu Bar**: Select **Tools → 🛡️ Antivirus & Security Settings…**
- **Preferences**: Select **Tools → ⚙️ Preferences…** and switch to the **🛡️ Antivirus & Security** tab.

### Configuring Pre-Download Security
1. In the **Pre-Download Safety Checks** section:
   - Check **"Inspect URLs before downloading"** to enable heuristic checks.
   - Check **"Warn before downloading executable files (.exe, .bat, .scr, etc.)"**.
   - Check **"Block dangerous URLs automatically"** if you want high-risk downloads stopped without prompting.
   - *(Optional)* Paste your **VirusTotal API Key** for online reputation lookups.

### Configuring Post-Download Scanning
1. In the **Post-Download Antivirus Scanning** section:
   - Check **"🛡️ Automatically scan completed files with antivirus"**.
2. Select your scanner:
   - **Windows Defender (Recommended)**: My-IDM detects `MpCmdRun.exe` automatically.
   - **Custom Antivirus Scanner Executable**:
     - Click **"Browse…"** to locate your antivirus CLI binary.
     - Specify scan arguments (default: `"%file%"`).
3. Choose **Action on threat**:
   - **Warn only (keep file, show notification)**
   - **Delete / Quarantine infected file immediately**
4. Click **"🧪 Test Antivirus Scanner"**:
   - Generates an ephemeral safe test file on disk and verifies that your scanner launches and reports clean status properly.

---

## Architecture & Code Reference

| Component | File | Description |
| :--- | :--- | :--- |
| **Security Config** | [`my_idm.security.SecurityConfig`](file:///d:/Projects/my-idm/my_idm/security.py) | Dataclass holding pre-scan and post-scan settings, VirusTotal API key, and `QSettings` persistence. |
| **URL Inspector** | [`my_idm.security.check_url_safety`](file:///d:/Projects/my-idm/my_idm/security.py) | Analyzes URLs for double extensions, high-risk types, bare IPs, and VirusTotal reputation. |
| **Defender Discovery** | [`my_idm.security.find_windows_defender_path`](file:///d:/Projects/my-idm/my_idm/security.py) | Scans standard Program Files and dynamic Windows Defender Platform folders for `MpCmdRun.exe`. |
| **File Scanner** | [`my_idm.security.scan_file`](file:///d:/Projects/my-idm/my_idm/security.py) | Invokes Windows Defender or custom CLI scanner with timeout handling and exit code parsing. |
| **Quarantine & Delete** | [`my_idm.security.quarantine_or_delete_file`](file:///d:/Projects/my-idm/my_idm/security.py) | Deletes or purges confirmed infected downloads from the filesystem. |
| **Download Pipeline** | [`my_idm.manager.DownloadManager`](file:///d:/Projects/my-idm/my_idm/manager.py) | Executes pre-download check before queueing and schedules post-download scan upon completion. |
| **UI Dialog** | [`my_idm.security_dialog.SecurityDialog`](file:///d:/Projects/my-idm/my_idm/security_dialog.py) | Dedicated dialog for adjusting and testing all antivirus parameters. |
| **Details Panel** | [`my_idm.details_panel.DetailsPanel`](file:///d:/Projects/my-idm/my_idm/details_panel.py) | Renders scan status badges and threat warnings in the download overview. |

---

## Scanning Is Tri-State

`scan_file()` returns a verdict that is **not** a boolean:

| Verdict | Meaning | Effect on the download |
| :--- | :--- | :--- |
| `True` | The scanner ran and found nothing | Completes, badge reads "✔ Clean (Scanned)" |
| `False` | The scanner found a threat | `threat_detected`; quarantined or deleted per **Action on Threat** |
| `None` | **No verdict was reached** — no scanner installed, misconfigured, timed out, crashed, or exited with an unreadable result | Completes, badge reads "⚠ Not scanned — <reason>" |

The `None` case used to report `True`. That was a fail-**open**: on every machine without Windows
Defender — that is, every Linux and macOS machine — each post-download scan reported success for a
file nothing had inspected, and the panel showed a green "Clean". A scanner that could not run says
nothing about the file, so the UI distinguishes "clean" from "not scanned" rather than collapsing
them.

The verdict must also never be `False` merely because it is falsy. `None` is falsy, so a call site
written as `if not verdict:` instead of `if verdict is False:` would route every unscanned download
into quarantine and, with **Action on Threat** set to *Delete*, remove the user's files. Both
post-download call sites in `manager.py` are written explicitly for this reason.

---

## Troubleshooting

- **No Scanner Available (Linux / macOS)**: The default scanner is Windows Defender, which does not
  exist off Windows. Nothing is scanned until you configure one — either point **Custom Antivirus
  Scanner** at `clamscan`/`clamdscan` (set **Arguments** to `--no-summary "%file%"`; exit `0` clean,
  `1` infected, `2` error), or switch to the **VirusTotal** engine for pre-download URL checks.
  Note `spctl` is *not* an antivirus scanner: it assesses code signing, so a non-zero exit is not a
  threat verdict.
- **Scanner Timed Out**: Large archives or multi-gigabyte disk images (`.iso`) may take longer to scan. The timeout is set to 90 seconds for Windows Defender and 60 seconds for custom scanners. A timeout now yields "not scanned" rather than "clean".
- **Custom Scanner Arguments**: the field is an **argument list**, not a shell command. Shell
  operators (`|`, `>`, `&&`) and redirections are not interpreted, and the target path is passed as
  a single argument regardless of spaces or quoting inside it. Pipelines belong in a wrapper script
  pointed at by **Executable**.
- **False Positives**: If a safe developer tool or self-compiled binary is flagged, check the **Warn only** option in settings to prevent immediate deletion.
- **VirusTotal Rate Limits**: Free public VirusTotal API keys are limited to 4 requests per minute. If you exceed this rate, the URL check gracefully defaults to local heuristic validation.
