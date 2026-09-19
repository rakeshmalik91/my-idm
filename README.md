<p align="center">
  <img src="assets/logo.png" alt="My-IDM Logo" width="160" height="160" />
</p>

<h1 align="center">My-IDM — Download Manager</h1>

<p align="center">
  <b>A modern, high-speed download manager with a sleek dark-themed GUI built in Python and Qt (PySide6).</b><br>
  <i>Segmented Parallel HTTP • BitTorrent Engine • VPN Kill Switch Privacy • Automated Malware Scanning</i>
</p>

---

## ✨ Features Overview

### 🚀 Core Download Engines
- **Segmented HTTP Downloads** — High-speed parallel multi-connection downloading (1–32 segments) with automatic fallback to single-stream.
- **Dynamic Filename Resolution** — Dynamically resolves and updates real filenames from HTTP `Content-Disposition` headers and URL redirects.
- **Torrent & Magnet Links** — Full BitTorrent protocol support via `libtorrent`, including magnet links, `.torrent` files, peer/seed stats, and fast-resume states.
- **Robust Pause & Resume** — Pause and resume any download. Automatically restores and resumes in-progress downloads upon restart or following unexpected system crashes.
- **Automatic Retries & Resilience** — Exponential backoff retry logic on network interruptions with configurable max retries.
- **Deduplication** — Detects duplicate URLs (HTTP) and info-hashes (Torrent), automatically resuming or focusing existing entries.

### 🛡️ Virus & Malware Protection
- **Pre-Download Safety Inspection**:
  - Warns before downloading dangerous executable and script formats (`.exe`, `.msi`, `.bat`, `.vbs`, `.scr`, `.iso`, etc.).
  - Detects and blocks social engineering attacks like **deceptive double extensions** (e.g. `file.pdf.exe`).
  - Optional **VirusTotal API** integration for cloud reputation scanning of target URLs prior to downloading.
- **Post-Download Antivirus Scanning**:
  - Automatically scans completed files in the background without freezing the user interface.
  - Native **Windows Defender** (`MpCmdRun.exe`) integration out of the box with zero setup.
  - Supports **Custom Antivirus Scanners** (ClamAV, Malwarebytes, ESET, etc.) with configurable arguments and `%file%` token templating.
  - Configurable action on threat detection: alert user or automatically quarantine (`.quarantine_malware`) / delete infected files.
  - **On-Demand Scanning**: Right-click any completed download and select **🛡️ Scan with Antivirus**.
  - Built-in **🧪 Test Antivirus Scanner** button to verify scanner configuration.

### 🌐 VPN, Tor, Proxy & Privacy Protection
- **Tor Network Privacy** — One-click toolbar toggle (`🧅 Tor: ON/OFF`) to route download traffic through Tor SOCKS5 proxy (`127.0.0.1:9050` or Tor Browser `127.0.0.1:9150`).
  - Configurable traffic routing: route standard HTTP/HTTPS downloads, BitTorrent swarms/trackers, or both.
  - Optional automatic activation on startup.
  - Live reachability test and connection indicator in status bar.
- **Network Interface Binding** — Bind all download traffic exclusively to a specific VPN network adapter (WireGuard, OpenVPN, Tailscale, TAP, etc.).
- **Kill Switch** — Real-time network watcher immediately pauses active transfers and blocks new downloads if the selected VPN interface disconnects.
- **Proxy Support** — Built-in HTTP and SOCKS5 proxy support with optional authentication, routing both HTTP segments and torrent traffic.
- **Live Status Bar Badges** — Clickable status bar badges show live Tor and VPN connection status and provide 1-click access to settings.
- **Built-in Connection Tester** — Verify Tor proxy, VPN adapter, and general proxy reachability directly within settings dialogs before applying.

- **Bottom Details Panel** — Splitter-based collapsible bottom pane (<kbd>F4</kbd> or Toolbar) with five tabbed inspection views:
  - **📋 Overview**: Comprehensive metrics (status, exact sizes, live down/up speeds, ETA, hashes, malware scan report, directory with 1-click open).
  - **📁 Files**: Multi-file inspection for torrents with interactive priority dropdowns (**High**, **Normal**, **Low**, **Don't Download**) that update the engine instantly.
  - **👥 Peers & Swarm**: Real-time BitTorrent swarm monitoring (peer IP, client software, transfer progress, download/upload rates, and protocol flags).
  - **📡 Trackers**: Live tracker tiers, announce URLs, connection status, and send statistics.
  - **🧩 Segments**: Parallel HTTP chunk visualization with byte ranges, downloaded sizes, and progress bars.
- **Unified Preferences & Settings Dialog** — Comprehensive preferences window (<kbd>Ctrl</kbd>+<kbd>,</kbd>, Toolbar **⚙️ Preferences**, or **Tools → ⚙️ Preferences…**) with tabs for General & Downloads, Network & VPN, and Antivirus & Security.
- **Configurable Default Download Directory** — Set your permanent download destination with directory browser, quick "Open Folder" button, and optional "Remember last used folder" mode.
- **Save as Default On The Fly** — Checkbox directly inside the Add Download dialog to instantly set the chosen directory as your new permanent default.
- **Unified Add Download Dialog** — Single dialog handles HTTP URLs, magnet links, and `.torrent` files with an integrated file browser.
- **Clipboard Auto-Prefill** — Opening Add Download automatically detects and prefills clipboard URLs, magnets, or `.torrent` file paths with pre-selected text for instant replacement.
- **Interactive Table & Smart Sorting** — Click any header to toggle ascending/descending sort (Added, Name, Size, Progress, Status, Speed, ETA, Completed). Preserves row selections during live updates.
- **Resizable Columns** — Interactively adjust column widths by dragging header dividers; column widths persist across sessions.
- **Compact & Clean Toolbar** — Modern icon-driven controls (▶ Play, ⏸ Pause, 🗑 Delete, 📂 Move, 🔄 Recheck, 📄 Open File, 📁 Open Folder, ⚙️ Preferences).
- **GitHub-Dark Aesthetic** — Modern dark palette with status color indicators (`Downloading` 🔵, `Completed` 🟢, `Scanning` 🛡️ Cyan, `Threat Detected` ⚠ Red, `Paused` 🟠, `Error` 🔴, `Seeding` 🟣).
- **File Management** — Move files to different directories without breaking tracking; open downloaded files or containing folders directly from the app.
- **Backlog File Automation** — Automatically batch-load URLs on startup from `~/.my-idm/backlog.txt` or import any `.txt` list via **File → Load Backlog** (`Ctrl+L`).

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| **GUI** | PySide6 (Qt 6) |
| **HTTP Engine** | `aiohttp` + `asyncio` |
| **Torrent Engine** | `libtorrent` (optional, falls back gracefully) |
| **Antivirus** | Windows Defender (`MpCmdRun.exe`) / Custom CLI engines |
| **Threat Intelligence** | VirusTotal API v3 |
| **Network & Privacy** | `psutil` + `urllib` + SOCKS5 / HTTP proxies |
| **Database** | SQLite3 (WAL mode) |

---

## 📥 Installation

```bash
# Clone the repository
git clone https://github.com/rakeshmalik91/my-idm.git
cd my-idm

# Install dependencies
pip install -r requirements.txt
```

Or install in editable development mode:
```bash
pip install -e .
```

---

## 🚦 Usage

### On Windows
Double-click `run.bat` or execute in terminal:
```cmd
run.bat
```

### Using Python
```bash
python -m my_idm.main
```

### Command-Line Arguments
```bash
python -m my_idm.main [OPTIONS]

Options:
  -b, --backlog FILE    Load URLs from a backlog text file on startup
  -v, --verbose         Enable debug logging in console and log file
  -h, --help            Show this help message and exit
```

---

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| <kbd>Ctrl</kbd> + <kbd>N</kbd> | Add Download (URL / Magnet / .torrent) |
| <kbd>Ctrl</kbd> + <kbd>T</kbd> | Add Torrent File |
| <kbd>Ctrl</kbd> + <kbd>R</kbd> | Resume Selected Download(s) |
| <kbd>Space</kbd> | Pause Selected Download(s) |
| <kbd>Delete</kbd> | Delete Selected Download(s) |
| <kbd>Enter</kbd> | Open Downloaded File |
| <kbd>Ctrl</kbd> + <kbd>O</kbd> | Open Containing Folder |
| <kbd>Ctrl</kbd> + <kbd>A</kbd> | Select All Downloads |
| <kbd>Ctrl</kbd> + <kbd>L</kbd> | Load Backlog File |
| <kbd>Ctrl</kbd> + <kbd>,</kbd> | Open Preferences / Settings |
| <kbd>Ctrl</kbd> + <kbd>Q</kbd> | Quit Application |

---

## ⚙️ Preferences & Settings

Access the unified preferences dialog anytime via **Tools → ⚙️ Preferences…**, the toolbar **⚙️ Preferences** button, or <kbd>Ctrl</kbd> + <kbd>,</kbd>:

- **📁 General & Downloads Tab**:
  - Set your permanent default download directory.
  - Browse for folders or open the active folder directly with the **📁 Open Folder** button.
  - Option to automatically remember the last used folder across downloads.
  - Configure default parallel segments for HTTP downloads (1–32, default 8).
  - Configure maximum concurrent active downloads (1–20, default 3).
  - Set maximum automatic retries on failure (1–10, default 5).
  - Toggle auto-resuming incomplete downloads on application startup.
  - Toggle completion notifications.
- **🌐 Network & VPN Tab**:
  - Adapter binding, real-time kill switch, and HTTP/SOCKS5 proxy settings.
- **🛡️ Antivirus & Security Tab**:
  - Pre-download URL safety rules, VirusTotal API, post-download antivirus scanning, and quarantine settings.

---

## 🛡️ Antivirus & Security Settings

Configure security policies via **Tools → 🛡️ Antivirus & Security Settings…**:

- **Pre-Download Safety Tab**:
  - Enable/disable safety checks before download.
  - Warn on executable/script downloads (`.exe`, `.msi`, `.bat`, etc.).
  - Block high-risk double extensions (`file.pdf.exe`).
  - Configure VirusTotal API key for cloud intelligence.
- **Post-Download Antivirus Tab**:
  - Select between **Windows Defender** or a **Custom Antivirus Scanner**.
  - Configure custom executable path and arguments using `%file%`.
  - Set remediation: **Alert user** or **Quarantine / Delete infected file**.
  - Click **🧪 Test Antivirus Scanner** to run an immediate test scan.

---

## 🌐 VPN & Network Settings

Configure network binding and privacy via **Tools → 🌐 VPN & Network Settings…** (or click the status bar badge):

- **Interface Binding**: Select your VPN adapter (`wg0`, `tun0`, `OpenVPN TAP`, etc.).
- **Kill Switch**: Automatically pause active downloads and reject new downloads if the VPN drops.
- **Proxy Configuration**: Configure HTTP or SOCKS5 proxy with optional host, port, username, and password.
- **Connection Test**: Click **🧪 Test Connection** to verify adapter connectivity and external IP.

---

## 📂 Data & File Locations

- **SQLite Database**: `~/.my-idm/downloads.db`
- **Application Logs**: `~/.my-idm/my-idm.log`
- **Torrent Resume States**: `~/.my-idm/fastresume/`
- **VPN Settings**: `~/.my-idm/vpn.json`
- **Security & Antivirus Settings**: `~/.my-idm/security.json`
- **Startup Backlog**: `~/.my-idm/backlog.txt`
- **Default Downloads**: `~/Downloads/`

---

## 📚 Detailed Documentation

- [**User Guide**](docs/user-guide.md) — Comprehensive guide on downloading, sorting, column resizing, VPN configuration, antivirus settings, and troubleshooting.
- [**Architecture Guide**](docs/architecture.md) — Deep-dive for developers: threading model, signal flow, database schema, engine internals, and adding new features.
- [**API Reference**](docs/api-reference.md) — Complete API documentation for all classes, methods, models, and signals.

### 🌟 Feature Guides
- [**VPN Interface Binding & Kill Switch Guide**](docs/vpn.md) — Hardware-level interface binding, leak prevention, kill switch monitoring, and proxy routing.
- [**Tor Network Privacy Guide**](docs/tor.md) — One-click Tor activation, automatic background `tor.exe` lifecycle management, SOCKS5 traffic routing, and diagnostic alerting.
- [**Antivirus & Malware Protection Guide**](docs/antivirus.md) — Pre-download URL safety heuristics, VirusTotal API, post-download Windows Defender & custom CLI scanner integration, and threat remediation.
- [**BitTorrent Swarm Engine Guide**](docs/torrent.md) — Magnet & `.torrent` handling, multi-peer pipelining, fastresume caching, interactive file prioritization, and swarm inspection.

---

## 📄 License

GPL-3.0-or-later