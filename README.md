<p align="center">
  <img src="my_idm/resources/logo.png" alt="My-IDM Logo" width="160" height="160" />
</p>

<h1 align="center">My-IDM — Download Manager</h1>

<p align="center">
  <b>A modern, high-speed download manager with a sleek dark-themed GUI built in Python and Qt (PySide6).</b><br>
  <i>Segmented Parallel HTTP • BitTorrent Engine • YouTube & Video-Site Downloader • VPN Kill Switch Privacy • Automated Malware Scanning</i>
</p>

<p align="center">
  <img src="media/screenshot.jpg" alt="My-IDM Main Interface" width="100%" />
</p>

---

> [!WARNING]
> **Active Development**: My-IDM is currently in active development. Features, interfaces, and configurations are subject to rapid evolution. Bugs, feedback, and contributions are welcome!

## ✨ Features

- 🚀 **Segmented HTTP & BitTorrent** — 1–32 parallel connections, magnet/torrent support, and crash auto-resume.
- 📺 **YouTube & Video-Site Downloader** — yt-dlp powered: paste a link, pick a quality, download video (merged via ffmpeg) or audio-only through My-IDM's own engine. Supports playlists and channels with checkboxes.
- 🌐 **Browser Integration** — Local unpacked Chrome extension for 1-click downloads, automatic intercept, and cookie forwarding.
- 🧅 **Tor & VPN Privacy** — 1-click Tor routing, network adapter binding, and instant kill switch protection.
- 🛡️ **Antivirus & Safety** — Pre-download deceptive extension blocks and background Windows Defender scans.
- 📋 **5-Tab Details Panel** — Collapsible inspector (<kbd>F4</kbd>) for live segments, torrent files, peers, and trackers.
- 🎛️ **Queue & UI** — Priority queue order (`#`), multi-column sorting, resizable columns, and dark theme.
- 🎬 **External Tools Integration** — Background AnimePahe scraper execution with real-time embedded console logs & browser sessions.

---

## 🛠️ Tech Stack

| Layer                   | Technology                                             |
| -------------------------| --------------------------------------------------------|
| **GUI**                 | PySide6 (Qt 6)                                         |
| **HTTP Engine**         | `aiohttp` + `asyncio`                                  |
| **Torrent Engine**      | `libtorrent` (graceful fallback)                       |
| **Video Extraction**    | `yt-dlp` (library mode) + `ffmpeg` for stream merging   |
| **Antivirus**           | Windows Defender (`MpCmdRun.exe`) / Custom CLI engines |
| **Network & Privacy**   | `psutil` + Tor SOCKS5 + HTTP/SOCKS5 Proxies            |
| **Database**            | SQLite3 (WAL mode)                                     |

---

## 📥 Installation & Usage

```bash
# Clone & install
git clone https://github.com/rakeshmalik91/my-idm.git
cd my-idm
pip install -r requirements.txt
```

Run the application:
```cmd
run.pyw               # Windows windowed launcher (no command prompt)
run.bat               # Windows console launcher
python -m my_idm.main # Or direct python execution
```

**Options:**
- `-b, --backlog FILE`: Batch load URLs from a text file on startup
- `-v, --verbose`: Enable debug logging

---

## 📺 Downloading YouTube & Video-Site Videos

My-IDM never speaks YouTube's protocols directly — all extraction is delegated to [`yt-dlp`](https://github.com/yt-dlp/yt-dlp), which is bundled as a Python library.

**Three ways in:**

1. **Paste** — open **Add Download** (<kbd>Ctrl</kbd>+<kbd>N</kbd>), paste a YouTube link, then click **Open YouTube Downloader** in the banner that appears.
2. **Menu** — **Tools → Download YouTube Video…** (<kbd>Ctrl</kbd>+<kbd>Y</kbd>).
3. **System tray** — right-click the tray icon → **➕ Add Download…**.

**How it downloads:** two engines are used, chosen automatically.

| Mode | Engine | When it is used |
|---|---|---|
| **A** | My-IDM's own HTTP engine | Audio-only streams. The CDN URL is handed to `HTTPEngine`, so you keep segmented, resumable, throttled and VPN/Tor-aware downloading. |
| **B** | `yt-dlp` + `ffmpeg` | Video. YouTube serves video and audio as *separate* streams that must be merged by ffmpeg. |

> [!NOTE]
> YouTube no longer offers combined video+audio files, so **video always uses Mode B**. Audio-only downloads use Mode A and get My-IDM's full download manager treatment.

**Playlists & channels** are listed with checkboxes. Listing one costs at most **two** network requests regardless of playlist size — one flat request for the list, plus one to resolve the first video's formats — and a quality preset applies to every selected video.

**Requirements:** `yt-dlp` is a declared dependency (`pip install -r requirements.txt`). `ffmpeg` is needed to merge video and audio; it is auto-detected on `PATH`, and its location plus the `yt-dlp` binary can be set in **Tools → Preferences → External Tools → YouTube**, which also offers an **Update yt-dlp** button and a live ✓/✗ validity check for both tools.

Private, members-only, age-restricted, and geo-restricted videos need a browser cookie source — also configurable in that settings tab. **Cookie access exposes your account credentials to yt-dlp, so use a throwaway account.**

See the **[YouTube Scraper Architecture](docs/architecture/youtube-scraper.md)** for the full design, including the Mode A/B decision logic and its measured API cost.

---

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| <kbd>Ctrl</kbd> + <kbd>N</kbd> | Add Download (URL / Magnet / .torrent) |
| <kbd>Ctrl</kbd> + <kbd>Y</kbd> | Download YouTube Video… |
| <kbd>Ctrl</kbd> + <kbd>R</kbd> | Resume Selected Download(s) |
| <kbd>Space</kbd> | Pause Selected Download(s) |
| <kbd>Delete</kbd> | Delete Selected Download(s) |
| <kbd>Enter</kbd> | Open Downloaded File |
| <kbd>Ctrl</kbd> + <kbd>O</kbd> | Open Containing Folder |
| <kbd>F4</kbd> | Toggle Details Panel |
| <kbd>Ctrl</kbd> + <kbd>,</kbd> | Preferences & Settings |
| <kbd>Ctrl</kbd> + <kbd>L</kbd> | Load Backlog File |
| <kbd>Ctrl</kbd> + <kbd>Q</kbd> | Quit Application |

---

## 📂 Configuration & Data Locations

All settings and runtime data are persisted in the user profile:
- **Database**: `~/.my-idm/downloads.db`
- **Resume Cache**: `~/.my-idm/fastresume/`
- **Application Logs**: `~/.my-idm/logs/my-idm.log`
- **Preferences**: Configurable via **Tools → Preferences** (<kbd>Ctrl</kbd>+<kbd>,</kbd>) or status bar badges.

---

## 🧪 Running the Tests

```cmd
run_all_tests.bat basic
run_all_tests.bat
```

There are two tiers, because the UI tests interfere with the desktop if you are using the
machine while they run.

| Command | What it runs | Time |
| :--- | :--- | :---: |
| `run_all_tests.bat basic` | Everything **except** the UI, system-tray and clipboard tests. Opens no window, steals no focus, and never touches your real clipboard — so you can leave it running in the background. | ~52 s |
| `run_all_tests.bat` or `run_all_tests.bat full` | The whole suite. **Run this before committing.** | ~3.5 min |
| `run_all_tests.bat ui` | Only the UI, tray and clipboard tests. | ~2 min 20 s |

---

## 📚 Documentation

- [**User Guide**](docs/user-guide.md) — Complete user manual: GUI navigation, download management, and troubleshooting.
- [**Architecture Guide**](docs/architecture/main.md) — System design, threading model (`asyncio` + `libtorrent` + Qt), and SQLite schema.
- [**Database & Persistence**](docs/architecture/database.md) — SQLite schema specification, constraints, indexing, `metadata_json` contract, migrations, and self-healing.
- [**Tor Privacy**](docs/architecture/tor.md) — SOCKS5 routing, daemon auto-discovery, startup gating, and exit termination.
- [**VPN & Kill Switch**](docs/architecture/vpn.md) — Network interface binding, adapter watcher loop, and proxy configuration.
- [**Antivirus & Security**](docs/architecture/antivirus.md) — Pre-download checks, Defender/custom scanning, and quarantine.
- [**BitTorrent Engine**](docs/architecture/torrent.md) — libtorrent integration, file priority mapping, and fastresume caching.
- [**Browser Integration (Chrome & Firefox)**](docs/architecture/browser-integration.md) — Unpacked Manifest V3 extension, loopback REST API, cookie forwarding, and download interception.
- [**Blob URL Handling**](docs/architecture/blob-urls.md) — Evaluation of browser `blob:` URLs, process-isolation limits, extension-assisted transfer, and native browser fallbacks.
- [**Capture (Hotkey & Clipboard)**](docs/architecture/capture.md) — System-wide hotkey that toggles download capture, clipboard URL capture with an all-or-nothing gate, and the shared `intercept_all` switch.
- [**Backlog Processing**](docs/architecture/backlog.md) — Batch queuing, multi-location discovery, custom locations & auto-clearing guidelines.
- [**YouTube Scraper**](docs/architecture/youtube-scraper.md) — yt-dlp integration, Mode A (direct URL) vs Mode B (ffmpeg merge), dialog, and rate-limit budgeting.
- [**Table Views & Segregation**](docs/architecture/table-views.md) — Column definitions and ordering, delegates, date-segregation algorithms, and the size/status/type header filters.
- [**Window Lifecycle & System Tray**](docs/architecture/window-system-tray.md) — Window geometry persistence, close-to-tray behaviour, and the tray context menu.
- [**State Machines**](docs/architecture/state-machines.md) — HTTP and BitTorrent state diagrams, transition matrices, and retry mechanics.
- [**Bandwidth Statistics**](docs/architecture/statistics.md) — Today / week / month / year / all-time totals from the existing `downloads` table, a `📊 Statistics` entry in **Tools** beside Preferences, a per-day volume chart and a live speed sparkline. Buckets are the user's **local** calendar days; rows are stamped in UTC and converted per row.
- [**Named Queues & Concurrency Budgets**](docs/architecture/queues.md) — Every download belongs to a named queue with its own concurrency ceiling, on top of the global limit. Scope the list with **Edit → Queues**; AnimePahe and YouTube get their own queues automatically; backlog files can assign queues with `queue=`.
- [**Cross-Platform Architecture**](docs/architecture/cross-platform.md) — Specifications for Linux (X11 & Wayland) and macOS (Intel & Apple Silicon) compatibility, including hotkeys, file managers, notifications, and XDG paths. Also the reference for launch-at-login registration on all three platforms.
- [**User Guide**](docs/user-guide.md) — End-user walkthrough of every feature, settings tab, and keyboard shortcut.
- [**API Reference**](docs/api-reference.md) — Comprehensive API reference for engines, models, and signals.

---

## 🤖 AI Development & Attribution

This project is actively developed with AI:
- **Models & Assistants:** 
  - Gemini 3.8 Flash, Claude 4.6 Opus (via Antigravity IDE)
  - Nvidia Nemotron 3 Ultra, Space Bunny Alpha, Poolside Laguna S 2.1 (via Kilo Code plugin for Antigravity IDE)

---

## 📄 License

GPL-3.0-or-later