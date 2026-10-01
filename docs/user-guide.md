# User Guide

Welcome to **My-IDM** — a full-featured download manager with a dark-themed GUI.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Adding Downloads](#adding-downloads)
  - [YouTube & Video-Site Downloads](#youtube--video-site-downloads)
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
- `ffmpeg` on your `PATH` (or configured in Preferences) — only needed to merge YouTube
  video + audio streams. Not required for audio-only or regular downloads.

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
| YouTube / video-site URL | `https://www.youtube.com/watch?v=…` | Routed to the YouTube downloader (see below) |

### YouTube & Video-Site Downloads

My-IDM does not implement YouTube's protocols itself — it delegates all extraction to
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp), and plays the result through one of two engines.

#### Opening the dialog

1. **Paste** — open **Add Download** (<kbd>Ctrl</kbd>+<kbd>N</kbd>) and paste a YouTube link. A banner
   appears: click **Open YouTube Downloader**.
2. **Menu** — **Tools → Download YouTube Video…** (<kbd>Ctrl</kbd>+<kbd>Y</kbd>).
3. **System tray** — right-click the tray icon → **➕ Add Download…**, or **ℹ️ About My-IDM**.

Supported link shapes include `youtube.com/watch?v=…`, `youtu.be/…`, `/shorts/…`, `/embed/…`,
`/live/…`, and `/playlist?list=…`.

#### Analyzing

Click **Analyze ▶**. The video's title, uploader, duration, upload date, and thumbnail are
fetched along with the list of available formats. For a playlist or channel you get a
checkbox list instead; **Select all** / **Clear** toggle it.

#### Choosing quality

| Preset | Selector | Notes |
|--------|----------|-------|
| Best available | `bestvideo*+bestaudio/best` | Default |
| Best 1080p / 720p / 480p / 360p | `bestvideo[height<=N]+bestaudio/best` | Caps the vertical resolution |
| Audio only (M4A / Opus) | `bestaudio[ext=…]/bestaudio` | No ffmpeg needed |

The format table labels each row with the engine that will handle it:

| Label | Meaning |
|-------|---------|
| `A (audio)` | Audio-only stream — downloaded by My-IDM's own engine, with segments, resume, throttle, and VPN/Tor support |
| `A (fast)` | A combined video+audio file (rare on YouTube) — also My-IDM's engine |
| `B (merge)` | Video-only stream — downloaded by yt-dlp and merged with ffmpeg |

> [!IMPORTANT]
> YouTube serves video and audio as **separate** streams and no longer offers combined files, so
> **video downloads always use yt-dlp + ffmpeg**. Only audio-only formats use My-IDM's engine.
> ffmpeg is auto-detected on your `PATH`; if it is missing, the dialog warns that merging is
> unavailable. Its location can be set in **Preferences → External Tools → YouTube**.

For playlists, the quality preset applies to **every** selected video — the per-format table is
hidden because only the first video's formats are resolved (listing a playlist costs at most two
requests regardless of how many videos it holds).

#### Options

- **Embed thumbnail** — writes the thumbnail into the file (needs ffmpeg).
- **Download and embed subtitles** — with a comma-separated language list, e.g. `en, ja`.
- **Save to** — defaults to the last used directory.

#### Deleting a video download

Deleting a YouTube download mid-transfer also removes yt-dlp's scratch files (`.part`, `.ytdl`,
and per-format `.fNNN` fragments) after waiting for the worker to release them. If a stray
`.part` file is ever left behind, close the app first — Windows locks files held by the worker.

#### Private, age-restricted, and geo-restricted videos

These need a cookie source. Set one under **Preferences → External Tools → YouTube → Cookie
source**, and route geo-restricted videos through **VPN** or **Tor** in
**Preferences → Network & Privacy**.

> [!WARNING]
> Reading browser cookies exposes your account credentials to `yt-dlp`. Use a throwaway account.

#### Updating yt-dlp

**Preferences → External Tools → YouTube → Update yt-dlp** runs `pip install -U yt-dlp`, or
`yt-dlp -U` when only the standalone binary is available. The same panel shows the detected
version and a live ✓/✗ for both `yt-dlp` and `ffmpeg`.

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

**Recheck is the way back from "File not found".** Unlike the other file actions (Open File, Open Folder, Move, Rename, Delete File, Malware Scan), which are greyed out for a `File not found` row because there is no file to act on, **Recheck stays enabled** — it is the one action that looks *at* the disk. Restore or move the file back and click Recheck: if it is complete the row returns to `Completed`, if it is partial it goes to `Paused` with the real byte count, and if it really is still gone the row resets to `Queued` with the reason shown.

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

1. Toggle **View → 🗂️ Segregated View → On** from the menu bar to enable or disable sectioned partitioning.
2. Select the grouping strategy directly under **View → 🗂️ Segregated View**:
   - **Status Grouping**: Segregates downloads into 3 collapsible sections:
     - **Active**: Items in `fetching metadata`, `queued`, `downloading`, `paused`, `stalled`, or `error` states.
     - **Seeding**: Items actively seeding in the BitTorrent swarm.
     - **Inactive**: Items in `completed`, `stopped`, `file_not_found`, or `suspended` states.
   - **Date Grouping**: Segregates downloads based on the latest of their `added_at`, `completed_at`, and `last_tried_at` timestamps into 5 collapsible sections:
     - **Today**: Active or completed today.
     - **Yesterday**: Active or completed yesterday.
     - **Last 7 Days**: Active or completed within the last 7 days.
     - **Last 30 Days**: Active or completed within the last 30 days.
     - **Older**: Older downloads or downloads without timestamps.
3. Click or double-click any section header row in the table to collapse or expand it (`▼` / `▶`).
4. Right-click any section header to expand/collapse all sections or quickly switch between Status and Date grouping.
5. The active segregation mode (`status` or `date`), enabled state, and individual collapsed section states are persisted in SQLite and restored automatically on next launch.
6. Date Grouping follows the calendar day, so if you leave My-IDM running past midnight the sections regroup themselves within a second — the rows move from `Today` to `Yesterday`, `Yesterday` to `Last 7 Days`, and so on, without restarting. Your selection and any collapsed sections are kept.

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

## System Tray & Background Execution

My-IDM integrates natively with the **Windows System Tray** (notification area) so your downloads and BitTorrent seeding can continue uninterrupted in the background.

### Tray Icon & Quick Controls
- **Left-click or Double-click**: Toggles the main window between hidden and restored.
- **Right-click Context Menu**:
  - **🪟 Show My-IDM / Hide My-IDM**: Toggle window visibility.
  - **⏸️ Pause All Downloads**: Instantly pause all ongoing, queued, and stalled transfers.
  - **▶️ Resume All Downloads**: Resume all paused and stopped transfers.
  - **⚙️ Preferences…**: Quick shortcut directly to Settings.
  - **ℹ️ About My-IDM**: Version, feature summary, and AI co-author credits.
  - **🔄 Restart My-IDM**: Restarts the application in place.
  - **🚪 Exit My-IDM**: Completely terminate My-IDM and stop all background services.

### Window Minimize & Close Behavior
1. **Minimize to Tray**: When enabled under Preferences, clicking the window minimize button hides My-IDM to the system tray instead of cluttering your taskbar.
2. **Close to Tray**: When enabled, clicking the window close (`X`) button keeps the application active in the tray with ongoing transfers and browser integration active. A one-time notification bubble confirms background execution.
3. **Completely Quitting**: To fully exit when close-to-tray is enabled, choose **File → Exit** (or right-click the tray icon and select **🚪 Exit My-IDM**).

---

## Preferences & Settings

Open the comprehensive preferences dialog anytime via:
- Toolbar: **⚙️ Preferences** button
- Menu: **Tools → ⚙️ Preferences…**
- Shortcut: **Ctrl+,**

The settings popup is organized into six tabs:

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
  - **Show desktop / status notification when a download completes**: Native Windows toast notification when transfers complete.
- **System Tray & Window Behavior**:
  - **Enable Windows system tray icon**: Displays the My-IDM icon in the Windows notification area with quick controls.
  - **Minimize window to system tray instead of taskbar**: Hides the window completely to the system tray on minimize.
  - **Close window to system tray**: Hides window on close (`X`), keeping downloads, seeding, and browser interception running uninterrupted in the background.
  - **Start My-IDM minimized to system tray**: Silently launches directly into the background on startup.
- **Backlog Files Auto-Processing**:
  - Auto-discover backlog files on startup and optionally clear processed URLs.

### 2. BitTorrent Tab
Configure seeding behavior after download completion, seeding time and ratio limits, maximum seeding speed, and startup seeding resumption.

### 3. Browser Integration Tab
Configure Chrome, Brave, Edge, Opera, and Mozilla Firefox browser integration:
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

### 4. Network & Privacy (VPN & Tor) Tab
Unified privacy and network routing tab combining VPN and Tor controls:
- **VPN Adapter Binding**: Bind downloads exclusively to a selected network interface (e.g., WireGuard, OpenVPN, TAP-Windows).
- **Kill Switch**: Automatically freezes and pauses all active downloads if the bound VPN adapter disconnects or drops, preventing IP leakage.
- **Proxy Server Configuration**: Configure standard HTTP or SOCKS5 proxies with optional authentication.
- **🧅 Tor Onion Routing & Privacy**:
  - **Enable Tor network routing (SOCKS5 proxy)**: Instantly routes download traffic through local Tor SOCKS5.
  - **Activate Tor automatically on startup**.
  - **Traffic Routing**: Choose to route HTTP/HTTPS web downloads, BitTorrent swarms/trackers, or both.
  - **Tor Port Presets**: Quick switch between Tor Service (`9050`) and Tor Browser (`9150`).
  - **Tor Executable (Optional)**: Specify or auto-discover `tor.exe` for silent background daemon launching.

### 5. Antivirus & Security Tab
Configure pre-download safety checks, executable warnings, double-extension blocking, and post-download antivirus scanning engines (Windows Defender or custom scanner).

### 6. External Tools Tab
Manage integration with external scrapers and download tools (e.g. AnimePahe Auto-Downloader).

Contains two groups:

**AnimePahe Auto-Downloader / Scraper** — repository path, launch-on-startup, periodic execution
with a configurable interval, and embedded console/debug log viewers.

**🎬 YouTube Downloader (yt-dlp)** — configuration for video-site downloads:

| Control | Purpose |
|---------|---------|
| Enable YouTube integration | Master switch; greys out the rest of the group. Disabling it also stops the Add Download banner from appearing for YouTube links. |
| yt-dlp path | Leave empty to auto-detect. Browse for a standalone binary or `pip`-installed copy. |
| ffmpeg path | Leave empty to auto-detect on `PATH`. Required to merge video + audio. |
| Live status | A ✓/✗ indicator per tool, refreshed whenever a path changes. |
| Version / Update yt-dlp | Shows the detected version; the button runs `pip install -U yt-dlp` (or `yt-dlp -U` for a binary install). |
| Default quality | The quality preset applied to new video downloads. |
| Prefer direct URL mode | Use My-IDM's engine when a single self-contained stream exists. In practice YouTube serves no combined streams, so video still uses ffmpeg merging. |
| Embed thumbnail / subtitles | Post-processing performed by yt-dlp (needs ffmpeg), with a subtitle language list. |
| Cookie source | Browser to extract cookies from, for private, members-only, age-restricted, or geo-restricted videos. |
| Auto-detect YouTube URLs | Show the YouTube hand-off banner when a YouTube link is pasted into Add Download. |
| Playlist entries to list | How many playlist/channel entries are listed per analysis. The limit affects only how many are *displayed* — a listing costs at most two requests regardless. |
| Extra yt-dlp args | Free-form arguments appended to every yt-dlp call. |

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

The main table shows 16 columns of information for each download:

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
| **File / Folder Name** | Resolved on-disk name (torrents fill this in once metadata is fetched) |
| **Last Seeded** | When the torrent last completed a seed. `—` for non-torrent downloads |
| **Source** | Where it came from: `Chrome`, `Firefox`, `Edge`, `AnimePahe`, or `YouTube`. Blank for manually added downloads |

> **Source** is derived automatically: browser downloads are identified from the User-Agent the
> extension sends, AnimePahe items from the marker it writes into the backlog file, and YouTube
> items from the yt-dlp download path. Downloads added by hand — and anything added before this
> column existed — simply show blank, which means "added manually".

> [!TIP]
> **Last Seeded** and **Source** are appended at the end of the table, after **Save Path**,
> **Source Domain**, and **File / Folder Name**. If you had a custom column layout saved from an
> earlier version, they are pinned to the end on first launch and your existing ordering is kept.
> **View → Reset View** restores the full default layout.

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

### Resizing Columns & Leaf-Node Path Shortening

All table columns are interactively resizable:
- Hover your mouse over the vertical divider line between any two column headers.
- Click and drag to adjust column widths to your preference.
- Your customized column widths are automatically saved and restored on subsequent application launches.
- **Smart Path Shortening**: In the **Save Path** column, when resized to narrower widths, paths automatically shorten while prioritizing leaf folders (e.g. `D:/.../TargetFolder` instead of cutting off the folder name).

### Resetting the View

If you ever wish to restore the default layout:
- Select **View → 🔄 Reset View** from the menu bar.
- This immediately resets all column widths to defaults, unhides any hidden columns, clears all status/type filters, and returns sorting to Date Added descending.


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
| **Ctrl+Y** | Download YouTube Video… |
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
| Log file | `~/.my-idm/logs/my-idm.log` |
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
