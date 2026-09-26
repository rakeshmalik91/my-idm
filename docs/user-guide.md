# User Guide

Welcome to **My-IDM** — a full-featured download manager with a dark-themed GUI.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Adding Downloads](#adding-downloads)
- [Managing Downloads](#managing-downloads)
- [Bottom Details Panel](#bottom-details-panel)
- [Preferences & Settings](#preferences--settings)
- [Browser Integration (Chrome / Brave / Edge / Firefox)](#browser-integration-chrome--brave--edge--firefox)
- [Dedicated Feature Guides](#dedicated-feature-guides)
- [VPN & Network Settings](#vpn--network-settings)
- [Antivirus & Malware Scanning](#antivirus--malware-scanning)
- [Backlog Files](#backlog-files)
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [Command-Line Options](#command-line-options)
- [Data & File Locations](#data--file-locations)
- [Troubleshooting](#troubleshooting)

---

## Getting Started

### Prerequisites

- Python 3.11 or later
- `pip` package manager

### Installation

```bash
# Install dependencies from requirements.txt
pip install -r requirements.txt
```

> **Note:** `libtorrent` is optional. If it cannot be installed on your system, the app will still work for HTTP downloads — torrent support will be disabled.

### Launch

```bash
python -m my_idm.main
```

The main window opens with an empty download list. The status bar at the bottom shows "Ready" and "0 download(s)".

---

## Adding Downloads

### Add a Download (URL, Magnet Link, or .torrent File)

1. Click **➕ Add Download** in the toolbar (or press **Ctrl+N**)
2. The dialog **automatically detects and prefills** any valid URL, magnet link, or `.torrent` file path found in your clipboard (with the text pre-selected for quick replacement).
3. For `.torrent` files, you can paste the file path directly or click the built-in **Browse .torrent …** button (or use File → **Add Torrent File…** / **Ctrl+T**).
4. Choose a save directory (prefilled with your configured default or last used directory). Check **"Set as default download folder"** to permanently save this folder as your new default.
5. Set the number of segments for parallel downloading (1–32, default 8 for HTTP)
6. Click **Download** (or press Enter)

### Supported Input Types

| Input | Example | Type |
|-------|---------|------|
| HTTP/HTTPS URL | `https://example.com/file.zip` | HTTP segmented download |
| Magnet link | `magnet:?xt=urn:btih:HASH&dn=name` | Torrent via libtorrent |
| `.torrent` file path | `D:\Torrents\file.torrent` | Torrent via libtorrent |

### Duplicate Detection

My-IDM automatically detects duplicates:

- **HTTP downloads** are matched by URL
- **Torrents** are matched by info-hash

If you add a URL that already exists:
- If **completed** → the add is silently ignored
- If **paused / errored / queued** → the existing download is automatically resumed
- If **already downloading** → no duplicate is created

---

## Managing Downloads

### Pause & Resume

- Select one or more downloads in the list
- Click **⏸** (Pause icon on toolbar) or press **Space** to pause
- Click **▶** (Play/Resume icon on toolbar) or press **Ctrl+R** to resume
- The Edit menu and right-click context menu also provide full text **Resume** and **Pause** actions.

Paused downloads save their progress to disk:
- **HTTP**: Per-segment byte offsets are saved to the database. On resume, only the remaining bytes are fetched.
- **Torrents**: Fast-resume data is saved to `~/.my-idm/fastresume/`. On resume, libtorrent picks up from where it left off.

### Delete

1. Select one or more downloads
2. Click **🗑 Delete** or press **Delete**
3. A confirmation dialog appears with the option **"Also delete downloaded files from disk"**
4. Click **Delete** to confirm

### Move

1. Select one or more downloads
2. Click **📂 Move** in the toolbar
3. Choose a new save directory
4. Click **Move**

Moving works without breaking active downloads:
- **HTTP**: The download is paused → file is physically moved → database is updated → download resumes
- **Torrents**: `libtorrent` handles `move_storage()` natively — no interruption needed

### Recheck

1. Select one or more downloads
2. Click **🔄 Recheck** in the toolbar

Rechecking verifies existing files:
- **Torrents**: Runs `force_recheck()` — libtorrent verifies every piece against its hash
- **HTTP**: Compares file size on disk vs. the expected total size. If the file is fully downloaded, it's marked as completed. If the file is missing, progress is reset.

### Open File / Open Folder

- **📄 Open File** (Enter) — Opens the downloaded file with the default application
- **📁 Open Folder** (Ctrl+O) — Opens the containing folder in Explorer with the file selected

### Export Selected as CSV

1. Select one or more downloads in the table.
2. Select **Tools → 📄 Export Selected as CSV…** or choose **Export Selected as CSV…** from the right-click context menu.
3. Choose a destination file. The CSV file is generated with two columns:
   - `Name`: Resolved/original download name.
   - `URL/Magnet`: Source direct URL or magnet URI.

### Segregated View & Collapsible Sections

1. Toggle **View → 🗂️ Segregated View** from the menu bar.
2. When enabled, downloads are segregated into 3 collapsible sections:
   - **Active**: Items in `fetching metadata`, `queued`, `downloading`, `paused`, `stalled`, or `error` states.
   - **Seeding**: Items actively seeding in the BitTorrent swarm.
   - **Inactive**: Items in `completed`, `stopped`, `file_not_found`, or `suspended` states.
3. Click or double-click any section header to collapse or expand it (`▼` / `▶`).
4. Section collapse states and the segregated view toggle are saved and restored automatically from the database.

### Context Menu

Right-click any download to access all actions (Pause, Resume, Recheck, Move, Rename, Export Selected as CSV, Open File, Open Folder, Delete).

---

## Bottom Details Panel

My-IDM includes a rich, collapsible bottom details panel separated from the main download table by an interactively resizable vertical divider (`QSplitter`).

### Opening and Toggling the Panel

- Press **F4** to toggle the panel on or off.
- Click **📋 Details Panel** on the toolbar or select **View → 📋 Details Panel** from the menu.
- Click **✕** in the top-right corner of the panel to close it.
- Your splitter sizes and open/closed visibility state are automatically remembered between app sessions.

### Tab Breakdown

When any download is selected in the main table, the panel updates dynamically across five dedicated tabs:

1. **📋 Overview**:
   - **Status & Progress**: Color-coded download state with error details and completion percentage.
   - **Size Metrics**: Exact downloaded bytes vs. total size formatted with human-readable binary units (KiB, MiB, GiB).
   - **Total Seeded / Uploaded**: Cumulative uploaded bytes and upload/download ratio for torrent transfers.
   - **Live Speeds**: Real-time download speed and upload speed indicators.
   - **ETA & Time Elapsed**: Precise remaining time calculation.
   - **Swarm & Transfer Breakdown**: Number of connected seeds and swarm peers (for torrents) or active parallel segments (for HTTP).
   - **Directory & Hash**: File path with an **Open Folder** shortcut, content hash, or torrent info-hash.
   - **Malware & Security Status**: Results of pre-download safety inspection or post-download antivirus scan.

2. **📁 Files**:
   - Inspect individual files included within multi-file BitTorrent transfers (or the target file for HTTP).
   - Columns: `#`, `File Name`, `Size`, `Progress Bar`, `Priority`, `Status`.
   - **Interactive Priority Control**: Change torrent file priorities on the fly via the dropdown:
     - **High** (priority 7) — Downloads first
     - **Normal** (priority 4) — Standard download priority
     - **Low** (priority 1) — Lower transfer priority
     - **Don't Download** (priority 0) — Skips downloading this file entirely

3. **👥 Peers & Swarm**:
   - Live peer list for BitTorrent transfers.
   - Columns: `IP Address : Port`, `Client Software` (e.g. qBittorrent, Transmission, libtorrent), `Peer Progress %`, `Download Speed`, `Upload Speed`, and swarm `Flags`.

4. **📡 Trackers**:
   - Complete list of announced BitTorrent trackers.
   - Columns: `Tier`, `Tracker URL`, `Status` (Working, Updating, Error), and `Send Stats`.

5. **🧩 Segments**:
   - HTTP parallel segment breakdown.
   - Columns: `Segment #`, `Byte Range` (start byte to end byte), `Downloaded Bytes`, `Progress Bar`, and `Status` (downloading, completed, pending).

---

## Preferences & Settings

Open the comprehensive preferences dialog anytime via:
- Toolbar: **⚙️ Preferences** button
- Menu: **Tools → ⚙️ Preferences…**
- Shortcut: **Ctrl+,**

The settings popup is organized into three tabs:

### 1. General & Downloads Tab

- **Default Download Location**:
  - Sets your permanent default download directory.
  - Click **Browse …** to choose any folder on your computer.
  - Click **📁 Open Folder** to open the current download directory directly in File Explorer.
  - **Remember last used folder when adding downloads**: When enabled, if you select a different directory in the Add Download dialog, My-IDM remembers that folder for subsequent downloads.
- **Download Performance & Engine Defaults**:
  - **Default parallel connections (segments)**: Configure the default number of HTTP segments (1–32, default 8).
  - **Maximum concurrent active downloads**: Limit simultaneous active downloads (1–20, default 3) to prevent saturating bandwidth.
  - **Maximum automatic retries**: Number of automatic reconnection attempts before marking a download as errored (1–20, default 5).
  - **Exponential backoff**: Toggle between exponential multiplier backoff and constant linear retry delay.
  - **Initial retry delay (s)**: Initial wait duration before retrying (0.1–120.0s, default 2.0s).
  - **Backoff multiplier**: Factor multiplied after each retry attempt (1.0–10.0x, default 2.0x).
  - **Maximum delay cap (s)**: Upper ceiling for exponential backoff wait times (1–3600s, default 60s).
- **Application Behavior**:
  - **Automatically resume incomplete downloads when application starts**: Interrupted or actively downloading items resume immediately on launch.
  - **Show desktop / status notification when a download completes**: Notifies you when files finish.

### 2. BitTorrent Tab
Configure seeding behavior after download completion, seeding time and ratio limits, maximum seeding speed, and startup seeding resumption.

### 3. Network & VPN Tab
Access adapter binding, kill switch, and HTTP/SOCKS5 proxy settings directly from the unified preferences window.

### 4. Tor Network Tab
Configure Tor SOCKS5 proxy routing, executable auto-discovery, and startup gating.

### 5. Antivirus & Security Tab
Configure pre-download safety checks, executable warnings, double-extension blocking, VirusTotal API key, and post-download antivirus scanning engines.

### 6. External Tools Tab
Manage integration with external scrapers and download tools (e.g. AnimePahe Auto-Downloader):
- **Repository Location**: Specify or auto-detect the path to the AnimePahe repository folder.
- **Launch on Startup (CLI mode)**: When enabled, My-IDM executes `animepahe_download.py --my-idm` in the background on startup. Discovered anime episodes are forwarded to the My-IDM backlog file and downloaded automatically.
- **Footer Status Badge & Quick Console Button**: While the AnimePahe scraper is actively running in the background, a green badge (`🎬 AnimePahe: Active`) and a quick button (`📄 Console Log`) are displayed in the main window status bar (footer):
  - **Clicking `📄 Console Log`**: Toggles the bottom details panel directly to the **🎬 AnimePahe Console** tab to actively stream live CLI scraper output in real time.
  - **Clicking `🎬 AnimePahe: Active`**: Displays a quick popup menu allowing you to:
    - View Console Logs in the bottom panel
    - Open Console Log in default external text editor (`console_log.txt`)
    - View Debug Logs (`debug_log.txt`)
    - Launch AnimePahe GUI
    - Stop or Start the background scraper process
    - Open External Tools Settings
- **Bottom Panel AnimePahe Console**: Provides a built-in terminal-like console viewer at the bottom of the window:
  - **Live Output Streaming**: Actively polls and appends stdout/stderr in real time as the CLI runs.
  - **Auto-Scroll & Text Wrap**: Keep pinned to the latest output or pause auto-scrolling to inspect earlier lines.
  - **Real-Time Log Filtering**: Filter output lines dynamically by typing into the search filter input.
  - **Controls**: Includes instant Clear, Open Log File, and Start/Stop Scraper buttons.
- **View Debug Logs**: Open `debug_log.txt` located in the AnimePahe repository folder.
- **Run CLI Now**: Start or stop the AnimePahe CLI scraper in background mode directly from Preferences settings.
- **Launch GUI**: Launch the standalone AnimePahe desktop GUI detached from My-IDM.

### 7. Browser Integration Tab
Configure Chrome, Brave, Edge, and Mozilla Firefox browser integration:
- **Enable Browser Integration**: Toggle the local HTTP loopback server (`127.0.0.1:19582`) on or off.
- **Port**: Configure loopback port (default `19582`).
- **Chromium Browsers Group (Chrome / Brave / Edge / Opera)**:
  - Copy-on-click inline links for `chrome://extensions/`, `edge://extensions/`, and the `browser_extension` folder path.
  - **📁 Open Extension Folder**: Opens Windows Explorer directly to the unpacked extension directory.
- **Mozilla Firefox Group**:
  - Copy-on-click inline links for `about:debugging#/runtime/this-firefox` and `manifest.json` (temporary mode).
  - Copy-on-click inline links for `about:config`, `xpinstall.signatures.required`, `false`, and `about:addons` (permanent mode).
  - **📦 Package Firefox Add-on (.xpi)**: Generates `my-idm-firefox.xpi` packaged specifically for Firefox.
  - **🦊 Permanent Firefox Guide**: Opens an interactive modal guide with full setup instructions for developer editions, privacy forks, and standard release AMO signing.
- **Interception Filters**:
  - **Automatically intercept downloads from Chrome/Edge/Firefox**.
  - **Intercept .torrent files from browser**.
  - **Intercept magnet links from browser**.
  - **Minimum file size to intercept (KB)** (0 = no limit; smaller files download directly via browser).
  - **Bypassed File Extensions**: Comma-separated list of extensions to ignore (e.g. `.crx, .pdf`).

---

## Browser Integration (Chrome / Brave / Edge / Firefox)

My-IDM includes an unpacked Manifest V3 browser extension that automatically intercepts downloads from Google Chrome, Brave, Microsoft Edge, and Mozilla Firefox (Gecko), routing them into My-IDM for accelerated, multi-segment downloading.

### 1. How It Works
- The extension runs locally in your browser.
- When you click a download link or start a download:
  1. **Chromium (Chrome/Edge/Brave/Opera)**: The extension listens on `chrome.downloads.onDeterminingFilename` to cancel the browser's download before writing to disk.
  2. **Mozilla Firefox**: The extension intercepts `downloads.onCreated`, cancels the native download job, and purges the browser history entry.
  3. It extracts the full session cookies for the domain using `chrome.cookies.getAll()` to preserve authenticated sessions.
  4. It sends an HTTP POST request to My-IDM's local REST server at `http://127.0.0.1:19582/add`.
  5. My-IDM starts downloading the file immediately with full multi-connection segmentation and authenticated cookies.

### 2. Loading the Unpacked Extension (No Store Required)

#### For Google Chrome / Brave / Edge / Opera:
1. In My-IDM, navigate to **Tools → Preferences → Browser Integration** (or locate the `browser_extension/` directory in the repository).
2. Click **Open Extension Folder** to locate the directory.
3. Open your browser and navigate to `chrome://extensions/` (or `edge://extensions/`).
4. Toggle **Developer mode** ON (top right switch).
5. Click **Load unpacked** (top left).
6. Select the `browser_extension/` directory inside your My-IDM installation.
7. Done! The My-IDM extension icon will appear in your browser toolbar.

#### For Mozilla Firefox:
- **Temporary Session (Quick Testing)**:
  1. Open Firefox and navigate to `about:debugging#/runtime/this-firefox` (or click **"🦊 Copy about:debugging"** in My-IDM Preferences).
  2. Click **Load Temporary Add-on...** and select `manifest.json` inside the `browser_extension/` directory.
  3. *Note: Mozilla Firefox purges temporary add-ons on browser restart by design.*
- **Permanent Installation (Retained Across Restarts)**:
  1. In My-IDM Preferences ➔ Browser Integration, click **"📦 Package Firefox Add-on (.xpi)"** to generate `my-idm-firefox.xpi`.
  2. Click **"🦊 Permanent Setup Guide"** to view instructions:
     - **Firefox Developer Edition / ESR / Floorp / LibreWolf**: In `about:config`, toggle `xpinstall.signatures.required` to `false`. Then in `about:addons`, click ⚙️ ➔ **Install Add-on From File...** and choose `my-idm-firefox.xpi`. It stays permanently across restarts!
     - **Standard Firefox Release**: Upload `my-idm-firefox.xpi` to [AMO Developer Hub](https://addons.mozilla.org/developers/addon/submit/distribution) (or view your builds at [AMO Version 6515778](https://addons.mozilla.org/en-US/developers/addon/84510108e17d4c599bce/versions/6515778)) for free automated unlisted signing (takes 1–2 minutes), then install the signed `.xpi`.

### 3. Usage & Features
- **Automatic Interception**: File downloads started in your browser are seamlessly sent to My-IDM.
- **Magnet Link Interception**: Clicking magnet links on any web page automatically forwards them directly to My-IDM without opening external browser prompt dialogs.
- **Context Menu Download**: Right-click any link, video, audio, or image and select **"Download with My-IDM"**.
- **Instant Bypass**: Hold down the <kbd>Alt</kbd> key when clicking a download link to bypass My-IDM and let your browser handle the download natively.
- **Cookie Jar Forwarding**: Session cookies from private logins, premium file hosts, and trackers are preserved seamlessly.

---

## Dedicated Feature Guides

For deep-dive documentation on specific features, refer to the dedicated feature guides:

- [**VPN Interface Binding & Kill Switch Guide**](vpn.md) — Hardware-level interface binding, leak prevention, kill switch monitoring, and proxy routing.
- [**Tor Network Privacy Guide**](tor.md) — One-click Tor activation, automatic background `tor.exe` lifecycle management, SOCKS5 traffic routing, and diagnostic alerting.
- [**Antivirus & Malware Protection Guide**](antivirus.md) — Pre-download URL safety heuristics, VirusTotal API, post-download Windows Defender & custom CLI scanner integration, and threat remediation.
- [**BitTorrent Swarm Engine Guide**](torrent.md) — Magnet & `.torrent` handling, multi-peer pipelining, fastresume caching, interactive file prioritization, and swarm inspection.

---

## VPN & Network Settings

My-IDM includes native support for binding downloads to specific VPN network interfaces (e.g. WireGuard, OpenVPN, NordLynx, Tailscale) with an automatic **Kill Switch**, as well as optional **SOCKS5/HTTP proxy** routing for both usual HTTP downloads and BitTorrent.

### Accessing Network Settings

- Click the network badge in the status bar (e.g., `🌐 Net: Default` or `🛡️ VPN: [Adapter]`), or
- Select **Tools → 🌐 VPN & Network Settings…** from the menu bar.

### Network Adapter / VPN Binding

1. **Adapter Selection**: Choose from automatically detected local adapters (VPNs are automatically badged with `🛡️ VPN`).
2. **Kill Switch Protection**: When enabled, My-IDM ensures that downloads will never leak over your default physical connection if the VPN disconnects. Downloads will halt with status `"VPN / Bound interface disconnected (Kill switch active)"` and wait for the VPN to reconnect.
3. **HTTP & Torrent Enforcement**:
   - **HTTP Downloads**: Bound via OS TCP connector directly to the selected adapter's local IP address.
   - **Torrents**: Libtorrent's `listen_interfaces` and `outgoing_interfaces` are locked strictly to the adapter's address.

### Proxy Support

- Supports **HTTP** and **SOCKS5** proxies (including optional user/password authentication).
- Routes both segmented HTTP streams and BitTorrent peer/tracker communication through the proxy.
- Built-in **🧪 Test Connection** button verifies proxy and adapter reachability before saving.

---

## Antivirus & Malware Scanning

My-IDM provides two-layer threat protection: **pre-download URL inspection** to protect against malicious sources before bytes are transferred, and **post-download antivirus scanning** to verify file integrity with your local antivirus engine.

Open the settings at **Tools → 🛡️ Antivirus & Security Settings…**.

### 1. Pre-Download Safety Inspection

Before initiating an HTTP download or torrent metadata fetch, My-IDM evaluates the target for known attack patterns and deceptive file naming:

- **Executable & Script Warning**: Alerts you when downloading `.exe`, `.msi`, `.bat`, `.vbs`, `.scr`, `.iso`, `.cmd`, `.ps1`, or other high-risk payload types.
- **Deceptive Double Extension Detection**: Automatically flags or blocks social-engineering tricks such as `invoice.pdf.exe` or `photo.jpg.scr`.
- **VirusTotal Online Reputation (Optional)**: If you enter your free VirusTotal API key, My-IDM can query 70+ online security engines for threat verdicts prior to downloading.

### 2. Post-Download Antivirus Scanning

Once a download finishes (for both HTTP segmented transfers and BitTorrent downloads):

- **Automatic Scan**: The download immediately transitions to **Scanning 🛡️** in the background without blocking the UI.
- **Windows Defender Integration**: On Windows systems, My-IDM automatically discovers and invokes `MpCmdRun.exe` (no extra configuration required).
- **Custom Antivirus Scanner Support**: Configure any third-party command-line antivirus scanner (e.g. ClamAV `clamscan.exe`, Malwarebytes, ESET) with custom argument templates using the `%file%` placeholder.
- **Action on Threat Detection**:
  - **Alert user (keep file)**: Flags status as **Threat Detected ⚠** and displays a security alert dialog with scanner diagnostics.
  - **Alert user and quarantine/delete**: Automatically isolates the malicious file (renames with `.quarantine_malware` extension or deletes) to prevent accidental execution.
- **Manual On-Demand Scan**: Right-click any completed download in the table and select **🛡️ Scan with Antivirus** to rescan at any time.

---

## The Download List

The main table shows 13 columns of information for each download:

| Column | Description |
|--------|-------------|
| **#** | Queue position for active downloads |
| **Name** | Filename (auto-updates when resolved from headers or metadata; hover for full path) |
| **Source Domain** | Hostname extracted from the source URL, magnet webseed, or tracker |
| **Size** | Total file size (human-readable, e.g. "1.5 GiB") |
| **Progress** | Color-coded progress bar with percentage |
| **Status** | Current state (see below) |
| **Speed** | Download speed (or upload speed when seeding) |
| **ETA** | Estimated time remaining |
| **Seeds / Peers** | For torrents: `S:5 P:12`. For HTTP: `8 seg` |
| **Added** | When the download was first added |
| **Last Tried** | When the last download attempt started |
| **Completed** | When the download finished |
| **Save Path** | Directory where the file is saved |

### Status Values

| Status | Color | Meaning |
|--------|-------|---------|
| Queued | Gray | Waiting to start or queued for retry |
| Downloading | Blue | Actively downloading |
| Paused | Orange | Paused by the user |
| Scanning 🛡️ | Cyan | Antivirus scan actively running on completed file |
| Completed | Green | Successfully finished and verified clean |
| Threat Detected ⚠ | Red | Antivirus detected malware or threat in file |
| Seeding | Purple | Torrent is seeding (uploading to others) |
| Error ⚠ | Red | Failed (hover for error details) |
| Checking | Orange | Rechecking file integrity |

### Progress Bar Colors

The progress bar changes color based on status:
- 🔵 **Blue** — Downloading
- 🔵 **Cyan** — Scanning with antivirus
- 🟢 **Green** — Completed (clean)
- 🟠 **Orange** — Paused
- 🔴 **Red** — Error or Threat Detected
- 🟣 **Purple** — Seeding

### Sorting the List

The download list supports full column sorting:

- **Default Sort**: By **Added** date, **Descending (DESC)** — newest downloads always appear at the top.
- **Click Column Headers**: Click any column header to sort by that column. Click again to toggle between Ascending (▲) and Descending (▼) order.
- **View Menu**: Use **View → Sort By** to select a sort column (Date Added, Name, Source Domain, Size, Progress, Status, Speed, ETA, Date Completed) and choose Ascending or Descending order.
- **Selection Preserved**: Sorting keeps your selected rows highlighted even when their row positions change.
- **Smart Sorting**: 
  - Downloads with active ETAs appear before inactive ones.
  - Completed downloads sort above uncompleted downloads when sorting by Completed date.
  - New downloads automatically insert into their proper sorted position without resetting the view.

### Resizing Columns

All table columns are interactively resizable:
- Hover your mouse over the vertical divider line between any two column headers.
- Click and drag to adjust column widths to your preference.
- Your customized column widths are automatically saved and restored on subsequent application launches.

---

## Backlog Files

A backlog file is a text file containing download URLs, magnet links, or `.torrent` file paths. My-IDM automatically discovers, processes, and manages backlog queues.

### Automatic Auto-Discovery & Periodic Polling

My-IDM continuously monitors and processes backlog files:
- **At Application Startup**: Backlog files are automatically scanned and queued on boot.
- **Periodic Background Polling**: By default, My-IDM periodically scans all configured locations every **60 seconds (1 minute)** for newly added URLs. This allows external scripts or downloads added to `backlog.txt` while the app is running to be queued automatically.
- **Configurable Interval**: You can adjust the polling frequency (5s to 3600s) or toggle it off under **Preferences → General → Backlog Files Auto-Processing**.

**Discovered Locations**:
1. **Project Directory / Working Directory**: `./backlog.txt`
2. **User Application Data Directory**: `~/.my-idm/backlog.txt`
3. **User Home Directory**: `~/backlog.txt`
4. **Custom Configured Places**: Any additional folders or specific files added under **Preferences → General**.

### Manual Loading

1. Click **File → Load Backlog** (or press **Ctrl+L**)
2. Select a `.txt` file
3. The status bar shows how many downloads were added

### Custom Download Locations

You can specify a custom destination folder for any download in a backlog file:

- **Per-Line Pipe Delimiter**: `https://example.com/file.zip | D:\Downloads\ISO`
- **Per-Line Arrow Delimiter**: `https://example.com/file.zip -> D:\Downloads\ISO`
- **Aria2 Style Inline Option**: `https://example.com/file.zip dir="D:\Downloads\ISO"`
- **Section / Directive Headers**: Set the default directory for all subsequent lines:
  ```txt
  # dir: D:\Torrents
  magnet:?xt=urn:btih:EXAMPLE_HASH&dn=example_file

  [D:\Media\Music]
  https://example.com/song.mp3
  ```

### Automatic Entry Clearing

By default, once entries from a backlog file are successfully queued or resumed, My-IDM automatically removes them from the file:
- If all downloads in the file are processed successfully, the backlog file is emptied (truncated to 0 bytes) so it won't be reprocessed on the next startup.
- If any downloads fail (e.g. invalid URL or connectivity error), failed lines are preserved in the file so you can inspect and fix them.
- This behavior can be toggled via the **"Clear entries from backlog file after processing successfully"** option in **Preferences → General**.

### Backlog File Format Example

```
# Lines starting with # are comments
# Empty lines are ignored

# 1. Standard HTTP downloads (uses default download folder)
https://example.com/file1.zip
https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso

# 2. Custom destination folder per download
https://example.com/driver.zip | D:\Drivers
https://example.com/dataset.tar.gz -> D:\Datasets

# 3. Section directive for subsequent items
# dir: D:\Torrents
magnet:?xt=urn:btih:EXAMPLE_HASH&dn=example_file
D:\Torrents\example.torrent
```

Duplicate URLs are automatically skipped or resumed. See [backlog.txt.example](../backlog.txt.example) for reference and the [Backlog Processing Architecture Guide](architecture/backlog.md) for full technical specifications.

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| **Ctrl+N** | Add Download (URL, Magnet link, or .torrent) |
| **Ctrl+T** | Add .torrent file directly |
| **Ctrl+R** | Resume selected downloads (Play) |
| **Space** | Pause selected downloads |
| **Ctrl+C** | Copy URL(s) / Magnet link(s) of selected download(s) |
| **F2** | Rename root file or folder (Torrent/HTTP) |
| **Delete** | Delete selected downloads |
| **Enter** | Open downloaded file |
| **Ctrl+O** | Open containing folder |
| **Ctrl+L** | Load backlog file |
| **Ctrl+,** | Open Preferences / Settings |
| **Ctrl+A** | Select all downloads |
| **Ctrl+Q** | Quit the application |

---

## Command-Line Options

```
usage: python -m my_idm.main [-h] [--backlog BACKLOG] [--verbose]

My-IDM — A full-featured download manager

options:
  -h, --help            show this help message and exit
  --backlog BACKLOG, -b BACKLOG
                        Path to a backlog file with URLs (one per line)
  --verbose, -v         Enable verbose (debug) logging
```

### Examples

```bash
# Launch normally
python -m my_idm.main

# Launch with a specific backlog file
python -m my_idm.main --backlog urls.txt

# Launch with debug logging
python -m my_idm.main -v

# Both
python -m my_idm.main -b urls.txt -v
```

---

## Data & File Locations

| Item | Path |
|------|------|
| Application data | `~/.my-idm/` |
| Download database | `~/.my-idm/downloads.db` |
| Log file | `~/.my-idm/my-idm.log` |
| Torrent fast-resume | `~/.my-idm/fastresume/` |
| Default backlog | `~/.my-idm/backlog.txt` |
| Default save directory | `~/Downloads/` |

> On Windows, `~` expands to `C:\Users\<username>`.

---

## How Downloads Work

### HTTP Downloads

1. A `HEAD` request is sent to discover the file size and check for `Accept-Ranges: bytes` support
2. **If range requests are supported**: the file is split into segments (default 8) and downloaded in parallel
3. **If range requests are NOT supported**: the file is downloaded in a single stream
4. If a segmented download fails (e.g. server returns 416 or 403 on a range request), the engine automatically falls back to a single-stream download
5. Each segment retries up to 5 times with exponential backoff (1s, 2s, 4s, 8s, 16s)
6. The file is pre-allocated to its full size on disk so segments can write to their respective offsets

### Torrent Downloads

1. The magnet link or .torrent file is added to the libtorrent session
2. For magnets, metadata is first resolved from the DHT/peers
3. The download proceeds using the BitTorrent protocol
4. After completion, the torrent remains in a seeding state until manually paused
5. Fast-resume data is saved on pause and shutdown for instant restart

### Automatic Retries

- HTTP segments and downloads retry according to configured retry settings (default 5 attempts).
- Exponential backoff calculates wait times dynamically (`delay = min(initial_delay * factor^(attempt), max_delay)`).
- When a download fails, its next retry timestamp is scheduled and the status shows a countdown message (e.g. `Retrying in 4s (attempt 2/5): Connection timed out`).
- A 1-second background queue monitor checks for queued downloads whose retry windows have elapsed.
- Downloads that exhaust all configured retries are marked as "Error".

### Resume on Restart

When the app starts, any downloads that were in "downloading" or "queued" state are automatically resumed.

---

## Troubleshooting

### "libtorrent not installed — torrent support disabled"

This means the `libtorrent` Python package could not be imported. Install it with:

```bash
pip install libtorrent
```

If pip cannot find a compatible wheel for your Python version, try installing Python 3.12 and using its pip:

```bash
py -3.12 -m pip install libtorrent
```

The app will still work for HTTP downloads without libtorrent.

### Download stuck at 0%

- Check that the URL is accessible (try opening it in a browser)
- Check `~/.my-idm/my-idm.log` for error details
- Some servers block range requests — the download should automatically fall back to single-stream

### Progress bar shows wrong percentage

Select the download and click **🔄 Recheck** to re-verify the file against its expected size.

### App won't start

Ensure all dependencies are installed:

```bash
pip install PySide6 aiohttp aiosqlite humanize
```

Run with verbose mode to see detailed logs:

```bash
python -m my_idm.main -v
```
