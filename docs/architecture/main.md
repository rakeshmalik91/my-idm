# Architecture Guide

Technical documentation for developers working on the My-IDM codebase.

---

## Table of Contents

- [Overview](#overview)
- [Module Map](#module-map)
- [Threading Model](#threading-model)
- [Data Flow](#data-flow)
- [Database Schema](#database-schema)
- [HTTP Engine Internals](#http-engine-internals)
- [Torrent Engine Internals](#torrent-engine-internals)
- [VPN & Network Privacy Architecture](#vpn--network-privacy-architecture)
- [Antivirus & Security Architecture](#antivirus--security-architecture)
- [Preferences & Configuration Architecture](#preferences--configuration-architecture)
- [Dynamic Filename Resolution & Crash Resilience](#dynamic-filename-resolution--crash-resilience)
- [GUI Architecture](#gui-architecture)
- [Key Design Decisions](#key-design-decisions)
- [Adding New Features](#adding-new-features)

---

## Overview

My-IDM follows a layered architecture with clear separation of concerns:

```
┌─────────────────────────────────────────────────┐
│                    GUI Layer                     │
│  MainWindow → DownloadTableModel → Delegates    │
│                    Dialogs                       │
├─────────────────────────────────────────────────┤
│               Manager Layer                      │
│         DownloadManager (QObject)                │
│   Orchestrates engines, DB, and GUI signals      │
├─────────────────────────────────────────────────┤
│               Engine Layer                       │
│     HTTPEngine          TorrentEngine            │
│   (asyncio/aiohttp)     (libtorrent)             │
├─────────────────────────────────────────────────┤
│               Data Layer                         │
│          Database (SQLite)                       │
│    DownloadEntry    SegmentEntry                 │
└─────────────────────────────────────────────────┘
```

---

## Module Map

```
my_idm/
├── __init__.py          # Package metadata, version string
├── main.py              # Entry point, CLI arg parsing, app setup
├── config.py            # General preferences (default directory, segments, concurrency)
├── database.py          # SQLite persistence, data classes
├── http_engine.py       # Segmented HTTP downloads with aiohttp
├── torrent_engine.py    # libtorrent wrapper for torrents
├── network.py           # Network interface info, proxy config, kill switch
├── network_dialog.py    # Network & VPN configuration dialog
├── security.py          # Antivirus scanners, URL inspection, quarantine
├── security_dialog.py   # Antivirus & malware security settings dialog
├── settings_dialog.py   # Unified Preferences dialog (General, Network, Security)
├── manager.py           # Central orchestrator (QObject with signals)
├── main_window.py       # QMainWindow with toolbar, menus, table, splitter
├── details_panel.py     # Bottom panel (Overview, Files, Peers, Trackers, Segments)
├── download_model.py    # QAbstractTableModel (12 columns)
├── delegates.py         # ProgressBarDelegate for QTableView
├── dialogs.py           # AddDownloadDialog, MoveDialog, DeleteDialog
└── styles.py            # Dark theme QSS stylesheet, color palette
```

### Dependency Graph

```
main.py
  ├── database.py
  ├── config.py
  ├── network.py
  ├── security.py
  ├── manager.py
  │     ├── database.py
  │     ├── config.py
  │     ├── network.py
  │     ├── security.py
  │     ├── http_engine.py
  │     │     └── database.py
  │     └── torrent_engine.py
  │           └── database.py
  ├── main_window.py
  │     ├── manager.py
  │     ├── settings_dialog.py
  │     ├── network_dialog.py
  │     ├── security_dialog.py
  │     ├── download_model.py
  │     │     ├── database.py (DownloadEntry)
  │     │     └── styles.py (Colors)
  │     ├── delegates.py
  │     │     └── styles.py (Colors)
  │     └── dialogs.py
  │           └── config.py
  └── styles.py
```

---

## Threading Model

My-IDM uses three execution contexts:

### 1. Qt Main Thread

- All GUI operations (widget updates, signal handling, user interaction)
- The `QApplication` event loop runs here
- `DownloadManager` lives here as a `QObject`
- Torrent polling timer (`_torrent_timer`, 1-second interval) runs here
- Retry timer (`_retry_timer`, 10-second interval) runs here

### 2. asyncio Background Thread

- A dedicated `threading.Thread` named `"idm-async"` runs an `asyncio` event loop
- All `aiohttp` I/O happens on this loop
- `HTTPEngine` coroutines run here
- Communication with the main thread:
  - **Main → Async**: `asyncio.run_coroutine_threadsafe(coro, loop)` to schedule downloads
  - **Async → Main**: Engine callbacks are invoked from the async thread; they call `DownloadManager` methods that emit Qt signals (which are cross-thread safe)

### 3. SQLite Thread Safety

- SQLite is opened with `check_same_thread=False` and `journal_mode=WAL`
- WAL mode allows concurrent reads with a single writer
- The `Database` class is accessed from both the main thread and the async thread
- Individual operations are atomic and committed immediately

```
┌──────────────────┐   signals    ┌──────────────────┐
│   Qt Main Thread │ ◄─────────── │  asyncio Thread  │
│                  │              │                  │
│  MainWindow      │  run_coro   │  HTTPEngine      │
│  DownloadManager │ ──────────► │  aiohttp tasks   │
│  TorrentEngine   │              │                  │
│  (polling timer) │              │                  │
└──────────────────┘              └──────────────────┘
         │                                │
         │        ┌──────────┐            │
         └───────►│  SQLite  │◄───────────┘
                  │   (WAL)  │
                  └──────────┘
```

---

## Data Flow

### Adding a Download

```
User clicks "Add URL"
  → AddDownloadDialog shown
  → User enters URL and clicks "Download"
  → MainWindow._on_add()
  → DownloadManager.add_download(url, save_path, segments)
    → Deduplication check: Database.find_by_url(url)
    → If duplicate and incomplete: resume existing entry
    → If new: Database.add_download(entry)
    → Emit download_added signal
    → _start_entry(entry):
        → If HTTP: asyncio.run_coroutine_threadsafe(http.add(entry))
        → If Torrent: torrent.add_torrent(entry)
  ← MainWindow._on_download_added(download_id)
    → DownloadTableModel.add_entry(entry)
```

### Progress Updates

```
# HTTP path:
aiohttp downloads chunk
  → HTTPEngine._emit_progress(id, downloaded, total, speed, eta)
  → DownloadManager._on_http_progress(...)  [callback, async thread]
  → Emit progress_updated signal           [cross-thread Qt signal]
  → MainWindow._on_progress_updated(...)   [main thread slot]
  → DownloadTableModel.update_progress(...)
  → dataChanged signal → QTableView repaints

# Torrent path:
QTimer fires every 1 second
  → DownloadManager._poll_torrents()
  → TorrentEngine.poll_all()
    → For each handle: handle.status()
    → TorrentEngine._progress_cb(...)
    → DownloadManager._on_torrent_progress(...)
    → Emit progress_updated signal
    → [same as HTTP from here]
```

### Automatic Retry Flow

```
Download fails with exception
  → HTTPEngine._run_download() catches it
  → HTTPEngine._handle_retry(entry, error_msg)
    → Database.increment_retry(id)
    → If retries < max_retries:
        → Database.update_status(id, "queued")
        → Emit status "queued"
    → Else:
        → Database.update_status(id, "error")
        → Emit status "error"

Every 10 seconds:
  → DownloadManager._process_retry_queue()
  → For each entry where status == "queued" and retry_count > 0:
    → _start_entry(entry)  [re-dispatch to engine]
```

---

## Database Schema

### `downloads` Table

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | TEXT PK | UUID | Unique download identifier |
| `url` | TEXT | — | Source URL or magnet link |
| `filename` | TEXT | `''` | Resolved filename |
| `save_path` | TEXT | `''` | Parent directory |
| `file_path` | TEXT | `''` | Full path to file on disk |
| `total_size` | INTEGER | `0` | Expected file size in bytes |
| `downloaded_size` | INTEGER | `0` | Bytes downloaded so far |
| `status` | TEXT | `'queued'` | Current state |
| `download_type` | TEXT | `'http'` | `http` or `torrent` |
| `num_segments` | INTEGER | `8` | Parallel connections (HTTP) |
| `error_message` | TEXT | `''` | Last error description |
| `retry_count` | INTEGER | `0` | Retry attempts so far |
| `max_retries` | INTEGER | `5` | Maximum retry attempts |
| `added_at` | TEXT | `''` | ISO 8601 timestamp |
| `last_tried_at` | TEXT | `''` | ISO 8601 timestamp |
| `completed_at` | TEXT | `''` | ISO 8601 timestamp |
| `etag` | TEXT | `''` | HTTP ETag for resume validation |
| `content_hash` | TEXT | `''` | File hash for integrity |
| `torrent_info_hash` | TEXT | `''` | BitTorrent info-hash |
| `metadata_json` | TEXT | `'{}'` | Extensible JSON blob |

### `segments` Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Segment identifier |
| `download_id` | TEXT FK | References `downloads.id` |
| `idx` | INTEGER | Segment index (0-based) |
| `start_byte` | INTEGER | First byte of this segment |
| `end_byte` | INTEGER | Last byte of this segment |
| `downloaded_bytes` | INTEGER | Bytes written so far |
| `status` | TEXT | `pending`, `downloading`, `completed`, `error` |

### `ui_state` Table

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT PK | Setting key (e.g. `window_state`) |
| `value` | TEXT | JSON-serialized or string state |

Persists window geometry (`x`, `y`, `width`, `height`), `is_maximized` state, table column lengths/widths (`column_widths`), QHeaderView state, details panel visibility, and splitter sizes across application launches.

### Indexes

- `idx_segments_download` on `segments(download_id)` — fast segment lookups
- `idx_downloads_url` on `downloads(url)` — deduplication by URL
- `idx_downloads_infohash` on `downloads(torrent_info_hash)` — deduplication by info-hash

### Data Classes

```python
@dataclass
class DownloadEntry:
    # Persisted fields (20 columns)
    id, url, filename, save_path, file_path, total_size,
    downloaded_size, status, download_type, num_segments,
    error_message, retry_count, max_retries, added_at,
    last_tried_at, completed_at, etag, content_hash,
    torrent_info_hash, metadata_json

    # Transient fields (not stored in DB, used by GUI)
    speed, eta_seconds, seeds, peers, upload_speed

    # Computed properties
    progress → float (0-100%)
    metadata → dict (parsed from metadata_json)

@dataclass
class SegmentEntry:
    id, download_id, index, start_byte, end_byte,
    downloaded_bytes, status
```

---

## HTTP Engine Internals

### Segmented Download Flow

```
_run_download(entry)
  │
  ├── _probe_url(url)           # HEAD request
  │     └── Returns: supports_range, total_size, etag, filename
  │
  ├── If supports_range AND total_size > 256 KiB:
  │     └── _segmented_download(entry, cancel_evt)
  │           ├── Load existing segments from DB (for resume)
  │           │   OR create new segments + pre-allocate file
  │           ├── For each incomplete segment:
  │           │     └── _download_one_segment(entry, seg, ...)
  │           │           ├── Range: bytes={start+downloaded}-{end}
  │           │           ├── Write chunks via f.seek(offset)
  │           │           ├── Update progress aggregate
  │           │           └── Retry loop (5 attempts, exp backoff)
  │           └── If _FallbackToSingle raised:
  │                 └── Delete segments, reset progress
  │
  └── Else (or on fallback):
        └── _single_download(entry, cancel_evt)
              ├── Resume from existing file size
              ├── Range: bytes={existing_size}-
              └── Append to file (or overwrite if server ignores range)
```

### Key Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `DEFAULT_SEGMENTS` | 8 | Number of parallel connections |
| `CHUNK_SIZE` | 64 KiB | Read buffer per iteration |
| `MAX_RETRIES_PER_SEGMENT` | 5 | Retry limit per segment |
| `RETRY_BASE_DELAY` | 1.0s | Exponential backoff base |
| `CONNECT_TIMEOUT` | 30s | TCP connection timeout |
| `READ_TIMEOUT` | 60s | Socket read timeout |

### Fallback Triggers

The segmented download falls back to single-stream when:
- `HEAD` response doesn't include `Accept-Ranges: bytes`
- Content-Length is unknown or too small (< 256 KiB)
- Any segment receives HTTP 416 (Range Not Satisfiable)
- Any segment receives HTTP 403 (Forbidden)
- Server returns 200 instead of 206 on a range request (segment index > 0)

### Pause/Resume Mechanism

**Pause:**
1. `cancel_evt.set()` — signals all active coroutines
2. Each segment saves its `downloaded_bytes` to the `segments` table
3. The active `aiohttp` task awaits completion (5s timeout, then cancel)
4. Status set to `"paused"`

**Resume:**
1. Load segments from DB with their saved offsets
2. Skip segments where `status == "completed"`
3. For each remaining segment, request `Range: bytes={start + downloaded_bytes}-{end}`
4. Continue writing at the correct file offset

---

## Torrent Engine Internals

### Session Lifecycle

```
start():
  lt.session_params() + lt.default_settings()
  → Set alert_mask (status | error | storage)
  → Create lt.session(params)

stop():
  → For each handle: save_resume_data()
  → Delete session
```

### Torrent Handle Management

| Operation | libtorrent Call |
|-----------|----------------|
| Add magnet | `lt.parse_magnet_uri(url)` → `session.add_torrent(params)` |
| Add .torrent | `lt.torrent_info(path)` → `session.add_torrent(params)` |
| Pause | `handle.pause()` |
| Resume | `handle.resume()` |
| Recheck | `handle.force_recheck()` |
| Move | `handle.move_storage(new_path)` |
| Remove | `session.remove_torrent(handle, [delete_files])` |

### Progress Polling

A `QTimer` fires every 1 second in the main thread:

```python
def poll_all():
    for download_id, handle in self._handles.items():
        s = handle.status()
        → total_size = s.total_wanted
        → downloaded = s.total_wanted_done
        → speed = s.download_rate
        → seeds = s.num_seeds
        → peers = s.num_peers
        → Emit progress callback
        → Check for completion
```

### Fast Resume

- On pause/stop: `handle.save_resume_data()` → save to `~/.my-idm/fastresume/{id}.fastresume`
- On start: if file exists, load resume data into `add_torrent_params.resume_data`

### Dynamic Metadata Resolution

When adding magnet links, torrent file metadata is initially absent:
1. The torrent enters the `downloading_metadata` state.
2. During 1-second polling ticks, the engine checks `handle.status().has_metadata`.
3. Once metadata is received from peers or DHT:
   - The engine reads `handle.torrent_file().name()`.
   - Updates the database `filename` and `total_size`.
   - Invokes `self._filename_cb(download_id, filename)`, which emits `manager.filename_resolved`.
   - The GUI table immediately updates the displayed name.

### Torrent Network & Proxy Enforcement

When network binding or proxying is configured:
1. `TorrentEngine.apply_network_config(network_cfg)` updates the active session settings pack.
2. Interface binding: sets `listen_interfaces` to `"{bind_ip}:6881"` and `outgoing_interfaces` to `"{bind_ip}"`.
3. Proxy routing: sets `proxy_type` (HTTP or SOCKS5), `proxy_hostname`, `proxy_port`, `proxy_username`, and `proxy_password` on the session.

---

## VPN & Network Privacy Architecture

My-IDM incorporates native network privacy controls to prevent IP leaks and enforce strict adapter routing:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        DownloadManager                                 │
│                                                                        │
│   NetworkConfig (QSettings: [Network])                                 │
│     ├── interface_name, interface_ip                                   │
│     ├── kill_switch (bool)                                             │
│     └── proxy_enabled, proxy_type, host, port, user, pass              │
└───────────────────┬─────────────────────────────────┬──────────────────┘
                    │                                 │
                    ▼                                 ▼
        ┌───────────────────────┐         ┌───────────────────────┐
        │      HTTPEngine       │         │     TorrentEngine     │
        │                       │         │                       │
        │ aiohttp.TCPConnector  │         │ lt.settings_pack      │
        │ local_addr=(ip, 0)    │         │ listen_interfaces     │
        │ proxy="http://..."    │         │ outgoing_interfaces   │
        └───────────────────────┘         └───────────────────────┘
                    ▲                                 ▲
                    │                                 │
                    └───────────────┬─────────────────┘
                                    │
                         [Kill Switch Watcher]
                         Monitors adapter status.
                         If bound interface drops:
                           1. Pauses all active downloads
                           2. Sets status: "VPN / Bound interface disconnected"
                           3. Suspends auto-retry queue
                           4. Updates GUI status bar badge
```

### Network Adapter Discovery & Classification

- **`my_idm/network.py:get_available_interfaces()`** uses `psutil.net_if_addrs()` and `psutil.net_if_stats()` to enumerate active network adapters.
- **VPN Identification**: Heuristic matching checks adapter names against `_VPN_KEYWORDS` (`vpn`, `wireguard`, `wintun`, `nord`, `tap`, `tun`, `tailscale`, `proton`, `mullvad`, `openvpn`, etc.) to automatically badge VPNs with `🛡️ VPN: [Adapter]`.

### Kill Switch Enforcement

1. **State Detection**: `is_interface_active(name, ip)` validates that the adapter exists, is flagged `isup`, and holds its assigned IP address.
2. **Transfer Interruption**: If the bound interface disappears or disconnects:
   - Active HTTP connections abort and mark status `"VPN / Bound interface disconnected (Kill switch active)"`.
   - Libtorrent session stops routing traffic.
   - `_process_retry_queue()` skips scheduling retries until the adapter returns.
3. **Status Bar Visual Feedback**: The clickable status bar badge dynamically updates between:
   - `🌐 Net: Default` (standard routing)
   - `🛡️ VPN: [Adapter]` (bound and active)
   - `⚠️ VPN: Disconnected` (traffic halted by kill switch)

### Proxy Integration & Live Connection Prober

- Supports **HTTP** and **SOCKS5** protocols with optional username/password authentication.
- **Connection Test**: `SettingsDialog._on_test_network()` performs an asynchronous HTTP GET against `https://httpbin.org/ip` using the bound interface address and proxy settings to display the external IP address and verify reachability before saving.

---

## Antivirus & Security Architecture

My-IDM incorporates a two-layer defense system against malware, dangerous scripts, and social engineering attacks:

```
                  ┌───────────────────────────────┐
                  │          User Input           │
                  │ (URL / Magnet / .torrent file)│
                  └───────────────┬───────────────┘
                                  │
                                  ▼
           [Pre-Download Safety Inspection (URL/Extension)]
             ├── Dangerous executable/script alert (.exe, .bat, etc.)
             ├── Deceptive double-extension check (.pdf.exe)
             ├── Bare IP hosting check
             └── VirusTotal API online threat lookup (optional)
                                  │
                                  ▼
                        Download Execution
                    (HTTP segmented / BitTorrent)
                                  │
                                  ▼
             [Post-Download Antivirus Scan (Background)]
             ├── Status set to 'scanning' (Cyan)
             ├── Background Daemon Thread (non-blocking)
             ├── Windows Defender (MpCmdRun.exe) or Custom CLI Scanner
             │     ├── Clean → Status set to 'completed' (Green)
             │     └── Threat → Status set to 'threat_detected' (Red)
             └── Action: Alert user / Quarantine (.quarantine_malware) / Delete
```

### Pre-Download Safety Inspection (`check_url_safety`)

Evaluates the download target before any payload bytes are transferred:
- **High-Risk Extension Blacklist**: Identifies `.exe`, `.scr`, `.bat`, `.cmd`, `.vbs`, `.js`, `.msi`, `.iso`, `.ps1`, etc.
- **Deceptive Double Extension Detection**: Flags files where an executable extension follows a document/media extension (e.g., `document.docx.exe`, `invoice.pdf.scr`).
- **Bare IP Addresses**: Warns if the host is a raw IP without a domain name (frequently used in malware distribution).
- **VirusTotal API v3 Integration**: Computes SHA-256 of the target URL, queries `/api/v3/urls/{id}`, and checks engine detection statistics.

### Post-Download Antivirus Scanning (`scan_file`)

Upon completion of either HTTP or Torrent downloads:
- **Non-Blocking Execution**: `DownloadManager._handle_completed_scan()` spawns a daemon thread (`threading.Thread`) so lengthy virus scans never stutter the GUI.
- **Windows Defender (`MpCmdRun.exe`)**:
  - Automatically discovered under `C:\Program Files\Windows Defender\MpCmdRun.exe` or `C:\ProgramData\Microsoft\Windows Defender\Platform\*\MpCmdRun.exe`.
  - Invoked with `-Scan -ScanType 3 -File "<path>" -DisableRemediation`.
  - Exit code `0` indicates clean; exit code `2` indicates a detected threat.
- **Custom Antivirus Scanners**: Allows configuring third-party tools (ClamAV, Malwarebytes, ESET) with tokenized argument strings using `%file%`.
- **Remediation**:
  - **Warn**: Marks status as `Threat Detected ⚠` (Red) and displays scanner diagnostic details.
  - **Quarantine**: Safely isolates the file by renaming it with the `.quarantine_malware` extension to prevent accidental execution.
  - **Delete**: Removes the infected payload from disk.
- **On-Demand Rescanning**: Users can right-click any completed download in the table and select **🛡️ Scan with Antivirus**.

---

## Preferences & Configuration Architecture

Configuration in My-IDM is centralized across three coordinated layers:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Preferences System                              │
├────────────────────────────────────────────────────────────────────────┤
│  GeneralConfig  │ default_save_path, remember_last, segments, retries  │
│  NetworkConfig  │ interface_name, interface_ip, kill_switch, proxy     │
│  SecurityConfig │ scan_before, scan_after, scanner_type, virustotal    │
├────────────────────────────────────────────────────────────────────────┤
│                          Storage Layer                                 │
│            QSettings("MyIDM", "My-IDM") / Registry & ini               │
│               [General]       [Network]       [Security]               │
└────────────────────────────────────────────────────────────────────────┘
```

### Effective Save Path Resolution

When a download is added without an explicit save path:
1. If `remember_last_save_path` is enabled and `last_save_path` is a valid directory, `last_save_path` is used.
2. Otherwise, `default_save_path` is used if valid.
3. Falls back to the operating system's standard `~/Downloads` directory.

### Unified Settings Dialog

- **`my_idm/settings_dialog.py:SettingsDialog`** provides a comprehensive 3-tab interface:
  - **📁 General & Downloads**: Default folder picker, "Open Folder" shortcut, segments, concurrency, and startup behavior.
  - **🌐 Network & VPN**: Interface picker, kill switch, and proxy configuration with connection test.
  - **🛡️ Antivirus & Security**: Pre-download rules, VirusTotal API, scanner selection, and scanner diagnostic test.
- Navigation shortcuts: `Tools → Preferences…` (<kbd>Ctrl</kbd>+<kbd>,</kbd>) opens tab 0; `Tools → VPN & Network Settings…` opens tab 1; `Tools → Antivirus & Security Settings…` opens tab 2.

---

## Dynamic Filename Resolution & Crash Resilience

### Dynamic Filename Resolution

Downloads often start with generic or hashed URLs (e.g. `/download?id=12345` or magnet info-hashes). My-IDM dynamically discovers and updates the true filename:
1. **HTTP Headers**:
   - Parses `Content-Disposition` according to **RFC 6266** and **RFC 5987** (`filename*="UTF-8''..."`).
   - Follows HTTP 3xx redirects to inspect the terminal URL path.
2. **Torrent Metadata**:
   - Resolves the true name once the `.torrent` metadata exchange completes.
3. **Reactive Signal Propagation**:
   - Engine calls `filename_cb(download_id, resolved_name)`.
   - `DownloadManager` emits `filename_resolved(download_id, resolved_name)` to Qt main thread.
   - `DownloadTableModel.refresh_entry()` updates the UI without resetting the table scroll or selection.

### Crash Resilience & Auto-Resume

- **SQLite WAL Mode**: State is continually flushed to `~/.my-idm/downloads.db`.
- **Per-Segment Offset Tracking**: Incomplete HTTP downloads store byte progress per segment.
- **Startup Recovery**: When `auto_resume_startup` is enabled in `GeneralConfig`, `DownloadManager.start()` queries SQLite for any downloads left in `"downloading"` or `"checking"` states and automatically resumes them without losing downloaded chunks.

---

## GUI Architecture

### Widget Hierarchy

```
QMainWindow (MainWindow)
  ├── QMenuBar
  │     ├── File (Add Download, Add Torrent, Load Backlog, Exit)
  │     ├── Edit (Resume, Pause, Delete, Move, Recheck)
  │     ├── View (📋 Details Panel [F4], Select All, Sort By)
  │     ├── Tools (Preferences, VPN & Network Settings, Antivirus & Security Settings)
  │     └── Help (About)
  ├── QToolBar
  │     └── [➕ Add Download] | [▶ Play][⏸ Pause] | [🗑 Delete][📂 Move][🔄 Recheck] | [📄 Open File][📁 Open Folder] | [📋 Details Panel][⚙️ Preferences]
  ├── QSplitter (central widget, vertical orientation)
  │     ├── QTableView (top pane)
  │     │     ├── Model: DownloadTableModel
  │     │     └── Delegate: ProgressBarDelegate (column 2)
  │     └── DetailsPanel (bottom pane, collapsible)
  │           ├── Header (Icon, Title, Transfer Type Badge, Open Folder, Close)
  │           └── QTabWidget
  │                 ├── Tab 0: 📋 Overview (Metrics grid: Status, Sizes, Speeds, ETA, Swarm/Segments, Path, Hash, Malware scan)
  │                 ├── Tab 1: 📁 Files (QTableWidget: #, Path, Size, Progress Bar, Priority QComboBox [High/Normal/Low/Skip], Status)
  │                 ├── Tab 2: 👥 Peers & Swarm (QTableWidget: IP:Port, Client, Progress %, Down Speed, Up Speed, Flags)
  │                 ├── Tab 3: 📡 Trackers (QTableWidget: Tier, URL, Status, Send Stats)
  │                 └── Tab 4: 🧩 Segments (QTableWidget: Segment #, Byte Range, Downloaded, Progress Bar, Status)
  └── QStatusBar
        ├── Status label
        ├── VPN / Network badge (clickable)
        ├── Speed label
        └── Count label
```

### Details Panel Architecture

The bottom details panel provides real-time, in-depth telemetry for the download selected in the primary table:

1. **Splitter & Resizing**:
   - `MainWindow` hosts `self._splitter = QSplitter(Qt.Orientation.Vertical)`.
   - The splitter position and panel visibility are preserved in `QSettings("MyIDM", "My-IDM")` via `splitter_state` and `details_visible`.
   - Users can toggle the panel with <kbd>F4</kbd>, the toolbar button, the `View` menu, or the panel's close button.

2. **Telemetry & Query Delegation**:
   - `DownloadManager` exposes inspection endpoints:
     - `get_download_files(download_id: str) -> list[dict]`
     - `set_torrent_file_priority(download_id: str, file_index: int, priority: int) -> bool`
     - `get_torrent_peers(download_id: str) -> list[dict]`
     - `get_torrent_trackers(download_id: str) -> list[dict]`
     - `get_download_segments(download_id: str) -> list[SegmentEntry]`
   - For torrents, `TorrentEngine` translates queries directly into libtorrent's `torrent_handle` APIs (`file_progress()`, `file_priorities()`, `get_peer_info()`, `trackers()`).
   - For HTTP downloads, `Database.get_segments()` returns active chunk ranges and downloaded byte counts.

3. **Live Sync & UI Performance**:
   - A dedicated 1-second `QTimer` in `MainWindow` drives periodic updates while keeping the UI responsive.
   - Signal connections on `_on_status_changed` and `_on_filename_resolved` trigger immediate refreshes when state changes occur.
   - When updating file tables, cell widgets (such as the priority `QComboBox` and `QProgressBar`) maintain their state to prevent cursor flickering or unnecessary re-renders.

### Model-View Architecture

```
DownloadTableModel (QAbstractTableModel)
  │
  ├── _entries: list[DownloadEntry]     # In-memory data
  ├── _id_to_row: dict[str, int]        # Fast lookup
  │
  ├── load_entries([entries])            # Full reset (startup)
  ├── add_entry(entry)                   # Insert row
  ├── remove_entry(download_id)          # Remove row
  ├── update_progress(id, ...)           # Efficient partial update
  ├── update_status(id, status)          # Status + color update
  └── refresh_entry(id, entry)           # Full row refresh
```

### Column Definitions

Defined in the `Col` class as integer constants:

```python
class Col:
    NAME = 0         # Filename or URL[:60]
    SIZE = 1         # humanize.naturalsize(total_size)
    PROGRESS = 2     # dict → ProgressBarDelegate
    STATUS = 3       # Capitalized status with color
    SPEED = 4        # Speed or upload speed
    ETA = 5          # Estimated time remaining
    TYPE = 6         # "HTTP" or "TORRENT"
    SEEDS_PEERS = 7  # "S:5 P:12" or "8 seg"
    ADDED = 8        # ISO → "YYYY-MM-DD HH:MM"
    LAST_TRIED = 9   # ISO → "YYYY-MM-DD HH:MM"
    COMPLETED = 10   # ISO → "YYYY-MM-DD HH:MM"
    SAVE_PATH = 11   # Directory path
```

### ProgressBarDelegate

The progress column uses a custom `QStyledItemDelegate` that:

1. Receives a `dict` with `{"progress": float, "status": str}` from the model
2. Draws a rounded rectangle background track
3. Fills a proportional rounded rectangle with a gradient based on status color
4. Overlays the percentage text in bold

Colors per status:
- Downloading → `#1f6feb` (blue)
- Completed → `#238636` (green)
- Scanning → `#39c5bb` (cyan)
- Threat Detected → `#da3633` (red)
- Paused → `#9e6a03` (amber)
- Error → `#da3633` (red)
- Seeding → `#8957e5` (purple)

### Interactive Table Sorting Architecture

`DownloadTableModel.sort(column, order)` implements data-type-aware comparator logic:
- **Col.NAME**: Natural case-insensitive string sorting.
- **Col.SIZE**: Numerical comparison based on `entry.total_size` in bytes.
- **Col.PROGRESS**: Float percentage comparison `entry.progress`.
- **Col.STATUS**: String comparison of localized status string.
- **Col.SPEED**: Float bytes-per-second comparison `entry.speed`.
- **Col.ETA**: Smart ETA sorting — active downloads with numeric ETA rank before inactive ones (`—`).
- **Col.ADDED / LAST_TRIED / COMPLETED**: Datetime-aware comparisons with uncompleted downloads sorted to the bottom.
- **Selection Preservation**: Before sorting, row IDs of selected items are remembered; after sorting, QItemSelectionModel re-selects the corresponding rows at their new visual indexes.

### Column Width Persistence

1. **Restoration**: On startup, `MainWindow._setup_table()` checks `QSettings("MyIDM", "My-IDM")` for `column_widths`. If present, applies each saved width to the table header.
2. **Persistence**: The header's `sectionResized` signal connects to `MainWindow._save_column_widths()`, saving interactive width adjustments immediately to `QSettings`.

### Signal Flow

```
DownloadManager signals:
  progress_updated(str, int, int, float, float, int, int, float)
    → MainWindow._on_progress_updated → model.update_progress

  status_changed(str, str, str)
    → MainWindow._on_status_changed → model.update_status

  filename_resolved(str, str)
    → MainWindow._on_filename_resolved → model.refresh_entry

  download_added(str)
    → MainWindow._on_download_added → model.add_entry

  download_removed(str)
    → MainWindow._on_download_removed → model.remove_entry

  download_moved(str)
    → MainWindow._on_download_moved → model.refresh_entry

  network_config_changed(object)
    → MainWindow._update_vpn_status_badge

  threat_detected(str, str)
    → MainWindow._on_threat_detected → QMessageBox warning
```

---

## Key Design Decisions

### Why asyncio in a separate thread?

Qt has its own event loop. Rather than using `qasync` (which can have compatibility issues), we run a vanilla `asyncio` loop in a daemon thread. Communication is achieved via:
- `asyncio.run_coroutine_threadsafe()` for main → async
- Qt signals (thread-safe) for async → main

### Why synchronous SQLite instead of aiosqlite?

The original plan included `aiosqlite`, but synchronous SQLite with `check_same_thread=False` is simpler and sufficient:
- Individual DB operations are fast (< 1ms)
- WAL mode prevents write locks from blocking reads
- The complexity of async DB access isn't justified for this workload

### Why callbacks instead of signals in engines?

The HTTP and Torrent engines use plain Python callbacks instead of Qt signals because:
- They run in non-Qt threads (asyncio loop)
- The manager translates callbacks → Qt signals for thread safety
- This keeps the engines decoupled from Qt

### Why pre-allocate files for segmented downloads?

The file is `truncate()`d to its full size before downloading begins. This allows multiple segments to `seek()` and write to their assigned offsets concurrently without conflicts, using `open(file, "r+b")`.

---

## Adding New Features

### Adding a New Column

1. Add a constant to `Col` in `download_model.py` and update `HEADERS` list
2. Add display logic in `DownloadTableModel._display_data()`
3. Set column width in `MainWindow._setup_ui()`
4. If needed, add a field to `DownloadEntry` in `database.py`

### Adding a New Download Engine

1. Create `my_idm/new_engine.py` with `start()`, `stop()`, `add()`, `pause()`, `resume()`, `cancel()` methods
2. Accept a `Database` instance and progress/status callbacks
3. Register it in `DownloadManager.__init__()`
4. Update `_detect_type()` and `_start_entry()` in `manager.py`
5. Add polling if the engine doesn't push updates

### Adding a New Action

1. Create a `QAction` in `MainWindow._setup_actions()`
2. Add it to the toolbar in `_setup_toolbar()` and/or menu in `_setup_menubar()`
3. Add it to the context menu in `_show_context_menu()`
4. Implement the handler method (e.g., `_on_new_action()`)
5. If it needs manager support, add a method to `DownloadManager`
