# Feature Guide: BitTorrent Swarm Engine & File Management

## Overview

My-IDM features a high-performance **BitTorrent Swarm Engine** powered by the industry-standard `libtorrent` library. It seamlessly handles `.torrent` files and `magnet:` links alongside standard HTTP/HTTPS downloads in a unified transfer manager.

With advanced multi-peer piece verification, DHT, Peer Exchange (PEX), selective file prioritization, live swarm inspection, and `.fastresume` state caching, My-IDM gives you full control over torrent downloads without requiring a standalone torrent client.

---

## Key Capabilities

### 1. Universal Magnet & Torrent File Support
- **Magnet URIs**: Full support for `magnet:?xt=urn:btih:...` links with embedded trackers and display names.
- **`.torrent` Files**: Add torrent descriptor files via the Add Download dialog, file picker, clipboard monitor, or direct drag-and-drop.
- **Asynchronous Metadata Resolution & Auto-Recheck**: While fetching metadata (`downloading_metadata` state), My-IDM connects to DHT nodes and swarm peers. Once decoded, it automatically renames the task, populates the file tree, and immediately triggers an on-disk piece recheck (`checking` state) to verify if the torrent payload or files already exist on disk before downloading.
- **Metadata Timeout & Suspended State**: Magnet links remaining in the `fetching_metadata` state longer than the configured timeout (`metadata_fetch_timeout_days`, default: `1` day) automatically transition to `'suspended'`.
  - The libtorrent handle is paused with `auto_managed` unset so it consumes 0 network bandwidth.
  - Its `queue_order` is reset to `0`, freeing up a concurrent download slot for queued downloads.
  - The `fetching_metadata_since` timestamp persists in the database across application restarts, ensuring time isn't reset on reboot.
  - A manual resume or successful progress (`downloaded > 0` or metadata resolution) resets the timer.

### 2. High-Speed Multi-Peer Pipelining
- **Swarm Discovery**: Leverages DHT (Distributed Hash Table), PEX (Peer Exchange), and LSD (Local Peer Discovery) to discover seeds and peers rapidly.
- **Live Performance Metrics**:
  - Download Speed & Upload Speed in real-time.
  - Connected Seeds vs Total Seeds (`Seeds (In Swarm)`).
  - Connected Peers vs Total Peers (`Peers (In Swarm)`).
  - Accurate dynamic ETA estimation based on moving average transfer rates.

### 3. Fastresume State Persistence
- Automatically caches session resume state under `~/.my-idm/fastresume/<id>.fastresume` using `lt.write_resume_data_buf()`.
- **Handle & Alert Pairing**: In multi-torrent sessions, libtorrent alerts (`save_resume_data_alert`) are strictly matched with their respective torrent handles and info hashes, preventing fastresume cross-contamination or entry swapping across app restarts.
- **Cryptographic Cache Integrity Check**: Upon loading fastresume on startup or resume, the cached metadata hash is validated against the magnet URI or `.torrent` file info hash. Any mismatched or stale cache is automatically discarded and unlinked.
- **Instant Resumption**: When restarting My-IDM or resuming paused torrents, files are verified using cached piece maps (`lt.read_resume_data()`), bypassing lengthy re-hashing of massive multi-gigabyte transfers.
- **Force Recheck**: Right-click any torrent and select **Force Recheck** to perform a complete piece-by-piece cryptographic hash validation against on-disk files.

### 4. Interactive Bottom Details Panel
Selecting any torrent download in the main table activates rich diagnostic tabs in the collapsible bottom panel:

#### A. Files Tab (Selective Downloading)
- Displays all files and folders contained in multi-file torrents.
- Columns: **File Name**, **Size**, **Progress Bar (%)**, and **Priority**.
- **Interactive Priority Control**: Change the priority dropdown for any individual file:
  - `High` (Priority 7): Downloads before normal files.
  - `Normal` (Priority 4): Default swarm priority.
  - `Low` (Priority 1): Lower piece scheduling preference.
  - `Don't Download` (Priority 0): Completely skips downloading the file, saving local disk space and bandwidth.
- Direct **Open File** and **Open Folder** action buttons.

#### B. Peers Tab (Swarm Topology)
- Live list of all connected peers with:
  - **Client Name**: Identifies peer software (e.g. `qBittorrent/4.6.3`, `Transmission/4.0.5`, `uTorrent/3.5.5`).
  - **IP & Port**: Remote peer socket address.
  - **Flags**: libtorrent peer status flags (`I` = Interested, `O` = Optimistic unchoke, `K` = Snubbed, `?` = Unknown).
  - **Down Speed** & **Up Speed**: Real-time per-peer transfer rates.
  - **Progress**: Percentage of the torrent that the peer currently possesses.

#### C. Trackers Tab
- Displays all tier-grouped trackers announced for the torrent:
  - **Tracker URL**: HTTP, HTTPS, or UDP announce endpoint.
  - **Tier**: Announce priority tier.
  - **Status**: Current health (`working`, `updating`, `unreachable`).
  - **Seeds & Peers**: Number of peers reported by each tracker.
  - **Next Announce**: Countdown timer to the next tracker update.

### 5. Seeding Lifecycle & Upload Bandwidth Control
- **Seeding State Transition**: Upon completing 100% download, torrents automatically transition to `'seeding'` (if configured in BitTorrent preferences) rather than stopping immediately.
- **Concurrency Management**: Seeding torrents do **not** consume active downloading concurrency slots (`max_concurrent_downloads`), allowing subsequent queued downloads to start without delay.
- **Seeding Bandwidth Controls**:
  - **Maximum Seeding Speed**: Direct upload rate cap in KB/s (0 = unlimited).
  - **Download-to-Seeding Speed Ratio**: Dynamically derives upload limit from the global download speed limit (`upload_limit = download_limit / ratio`, e.g. `2:1`).
  - The effective upload rate applied to libtorrent handles (`handle.set_upload_limit(...)`) automatically chooses the lowest non-zero cap among configured limits.
- **Dedicated Preferences Tab**: BitTorrent-specific settings (seeding behavior, upload limits, ratio, metadata timeout, and `.torrent` file integration) are organized in a dedicated **🧲 BitTorrent** tab in the Preferences window (`Ctrl+,` or `Tools -> BitTorrent Settings…`).

### 6. Privacy & Network Integration
- **VPN Binding & Kill Switch**: If interface binding is enabled in [VPN Settings](file:///d:/Projects/my-idm/docs/vpn.md), libtorrent binds both `listen_interfaces` and `outgoing_interfaces` strictly to the VPN adapter IP. If the VPN drops, torrent transfers freeze instantly.
- **Tor Network Routing**: When Tor routing is enabled in [Tor Settings](file:///d:/Projects/my-idm/docs/tor.md), all peer connections and tracker announces are forced through the Tor SOCKS5 proxy (`force_proxy=True`, `proxy_peer_connections=True`, `proxy_tracker_connections=True`).

---

## User Interface & Controls

### Adding a Torrent
1. Click **➕ Add URL** on the toolbar or press `Ctrl+N`.
2. Paste a `magnet:?xt=...` link into the URL field, or click **"📁 Browse Torrent…"** to select a `.torrent` file from your disk.
3. Choose the target destination directory (or use the configured default download folder).
4. Click **"OK"**. The torrent starts downloading immediately.

A `.torrent` can also arrive without the dialog at all — see below.

---

## `.torrent` Files From the System

A `.torrent` is the one download kind the user holds as a *file* rather than a URL, so it has three
routes in that never touch Add Download. All three funnel through
`DownloadManager.add_torrent_files`, which owns the ingestion policy so they cannot disagree.

### Drag and drop, anywhere in the window

Drop a `.torrent` on the table, the details panel, the toolbar or the status bar. `MainWindow`
overrides `dragEnterEvent` / `dragMoveEvent` / `dropEvent`; `setAcceptDrops(True)` is armed in
`showEvent` so one window-level handler covers the whole window (Qt propagates a drag up the parent
chain until something accepts, and no child accepts).

`dragEnterEvent` decides on the *payload*, not the drop, so a drag with nothing usable is ignored
and never lights up a drop cursor. Only existing local `.torrent` files are taken; a mixed
selection adds its torrents and ignores the rest. A `file://` URL goes through
`QUrl.toLocalFile()` — a raw `file://` string does not survive `_detect_type` and would quietly
become an HTTP download.

Two regions are **not** covered, structurally rather than by oversight:

- **The embedded browser.** `DetailsPanel` reparents a native Chrome HWND into a Qt container, and
  a non-Qt window generates no Qt drag events at all.
- **Modal dialogs.** Add Download and Preferences are separate top-level windows, so a drop onto
  them is delivered to them.

### A watched folder

Preferences → BitTorrent → **.torrent Files from the System**. `TorrentFolderWatcher` adds
`.torrent` files that appear in a chosen folder. The folder defaults to the effective default
download folder, resolved live so a later change to that setting is followed rather than pinned.

Three properties make it safe to point at a real downloads directory:

- **Age-limited, not just new-file-limited.** Only files modified within
  `MAX_TORRENT_AGE_DAYS` (3) are considered. A folder holding a hundred torrents from years past
  imports nothing and starts nothing, while a file that arrived while the app was closed is still
  picked up on the next start.
- **A file must stop changing before it is believed.** A `.torrent` dropped into a watched folder
  is usually still being copied, and a truncated one parses into an `error` row the user has to
  delete. A file is reported only once its size is unchanged across two observations at least
  `WATCH_STABILITY_MS` apart.
- **A rescan never restarts a paused torrent.** `add_download` treats a re-add of a paused row as
  a request to *resume* it — right for a deliberate Ctrl+V, badly wrong for a watcher that
  rescans every minute. The watched folder passes `only_new=True`, which skips any path already
  in the table without touching it.

`QFileSystemWatcher` is used for responsiveness but is never trusted alone: events are missed
across suspend/resume and cannot be watched on a network drive, so a periodic rescan
(`WATCH_RESCAN_MS`) is what makes the feature dependable. The watcher is only ever started when the
manager's asyncio thread is alive, so a manager built by a test or a headless import never scans
the user's disk — the same invariant `_apply_backlog_timer_config` keeps for the backlog poll.

### File association

Preferences → BitTorrent → **Open .torrent files with My-IDM** (`associate_torrent_files`).
`my_idm/file_assoc.py` mirrors `autostart.py`: one platform backend chosen at import, a `status()`
comparing what is registered against what *this* build would register, and a public API that never
raises.

**An application cannot make itself the default handler.** This is the central fact of the feature:

| Platform | What can be done | Reported state |
| :--- | :--- | :--- |
| Windows | Write `HKCU\Software\Classes\.torrent` + a `MyIDM.Torrent.1` ProgID under the *current user* (no elevation). Makes My-IDM appear in the file's **Open with** list. | `REGISTERED` normally; `DEFAULT` only after the user picks My-IDM in Default Apps |
| Linux | Write a desktop entry and run `xdg-mime default` — the one platform where the application really may set the default. | `REGISTERED` / `DEFAULT` |
| macOS | Nothing. LaunchServices reads `CFBundleDocumentTypes` from the bundle's `Info.plist`, which an unbundled Python application cannot reach. | `UNSUPPORTED`, pointing at Finder's Get Info |

On Windows the user's choice lives in `...\FileExts\.torrent\UserChoice`, whose value is a hash the
user generates by clicking through Default Apps; a program writing that key has its change silently
discarded. So the checkbox records intent, `file_assoc` owns the registration, and the status line
reports the **real** state — `REGISTERED` is deliberately distinct from `DEFAULT` and says so, with
a button that opens the Default Apps page. Reporting that as "on" is the same lie
`_refresh_autostart_status` avoids for the login item.

Two details that are easy to get wrong and are covered by tests:

- The registered command is `autostart.app_command()` — **not** `launch_command()`. The latter
  carries `--autostart`, which suppresses the main window whenever a tray is available, so a
  double-clicked `.torrent` would be added invisibly. Single-instance forwarding already handles the
  case where My-IDM is already running.
- `reconcile()` is called only when the checkbox actually *moved*, mirroring the launch-at-login
  control. Reconciling unconditionally would resurrect an association the user removed in the
  system settings merely because they opened Preferences and pressed Save.

`is_enabled()` answers `False` for `STALE`, which is load-bearing: `reconcile` short-circuits on it,
so a broken entry answering `True` would make Repair a no-op and the stale command would survive
forever.

---


### Managing Active Torrents
- **Pause / Resume**: Click the toolbar buttons or right-click the row in the table.
- **Force Recheck**: Right-click a paused or completed torrent and select **Force Recheck** to re-hash on-disk files.
- **Adjusting File Priorities**:
  1. Click the torrent row in the main table.
  2. In the bottom Details Panel, switch to the **Files** tab.
  3. Select any file's **Priority** dropdown and choose **Don't Download** to skip it, or **High** to prioritize it.
- **Viewing Swarm Health**: Switch to the **Peers** or **Trackers** tabs in the bottom panel.

---

## Architecture & Code Reference

| Component | File | Description |
| :--- | :--- | :--- |
| **Torrent Engine** | [`my_idm.torrent_engine.TorrentEngine`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Core engine wrapping `libtorrent.session`, managing handles, DHT/PEX, alerts, and periodic ticks. |
| **Fastresume Cache** | [`FASTRESUME_DIR`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py#L43) | Directory `~/.my-idm/fastresume/` storing bencoded resume buffers for instant startup. |
| **Priority Controller** | [`set_torrent_file_priority`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Dynamically applies `lt.torrent_handle.file_priority()` across individual files. |
| **Swarm Diagnostics** | [`get_torrent_peers`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) / [`get_torrent_trackers`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Queries `get_peer_info()` and `trackers()` for UI presentation. |
| **Details Panel** | [`my_idm.details_panel.DetailsPanel`](file:///d:/Projects/my-idm/my_idm/details_panel.py) | Multi-tab inspection panel rendering files, peer tables, tracker metrics, and progress bars. |
| **Download Manager** | [`my_idm.manager.DownloadManager`](file:///d:/Projects/my-idm/my_idm/manager.py) | Coordinates download lifecycle, transitions, speed aggregation, and database persistence. |
| **Network Binding** | [`TorrentEngine.apply_network_config`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Sets `outgoing_interfaces` and `listen_interfaces` on `libtorrent.session_settings`. |
| **File Ingress** | [`my_idm.torrent_sources`](file:///d:/Projects/my-idm/my_idm/torrent_sources.py) | `.torrent` paths from a drag payload, and the watched-folder scanner. |
| **Ingestion Policy** | [`add_torrent_files`](file:///d:/Projects/my-idm/my_idm/manager.py) | The one add path for button, drop and watched folder; canonicalises and de-duplicates. |
| **File Association** | [`my_idm.file_assoc`](file:///d:/Projects/my-idm/my_idm/file_assoc.py) | Per-platform `.torrent` handler registration, and the honest report of what the OS will actually do. |
| **Tor Routing** | [`TorrentEngine.apply_tor_config`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Configures SOCKS5 proxy and privacy flags on `libtorrent.session_settings`. |
| **State Machine & Lifecycle** | [`docs/architecture/state-machines.md`](file:///d:/Projects/my-idm/docs/architecture/state-machines.md) | Dedicated BitTorrent state diagram, metadata timeout lifecycle, seeding rules, and slot allocation. |

### `.torrent` file integration settings

All five live in `TorrentConfig` and default safely — each acts on the machine outside the app
window, so nothing is opted into until asked for.

| Key | Default | Description |
| :--- | :--- | :--- |
| `associate_torrent_files` | `False` | Register My-IDM as a `.torrent` handler. Intent only; `file_assoc` owns the registration. |
| `watch_torrent_folder` | `False` | Watch a folder and add `.torrent` files that appear in it. |
| `torrent_watch_folder` | `""` | The folder to watch. `""` means the effective default download folder, resolved live. |
| `clean_watched_torrent_files` | `False` | Automatically move `.torrent` files to trash after adding them from the watched folder. |
| `torrent_watch_max_age_days` | `3` | Maximum age in days for `.torrent` files picked up from the watched folder (0 = unlimited). |

`WATCH_RESCAN_MS`, `WATCH_DEBOUNCE_MS` and `WATCH_STABILITY_MS` are module constants in
`my_idm/torrent_sources.py` rather than settings — they are correctness bounds, not preferences.

---

## Troubleshooting

- **"libtorrent not installed" Warning**:
  - My-IDM gracefully degrades to HTTP/HTTPS downloads if `libtorrent` is not installed.
  - To enable BitTorrent support, run:
    ```bash
    pip install libtorrent
    ```
- **Stuck on "Downloading metadata"**:
  - Magnet links require discovering at least one active peer in the swarm to retrieve the `.torrent` metadata dictionary.
  - If a torrent has very few seeds or uses dead trackers, discovery may take several minutes. Ensure UDP and DHT traffic are not blocked by a firewall.
- **Skipped Files Still Created on Disk**:
  - BitTorrent operates on fixed-size cryptographic pieces (often 1 MB–16 MB). If a piece spans the boundary between a skipped file and a requested file, libtorrent must allocate the border chunk on disk to verify data integrity.
- **Port Forwarding**:
  - While My-IDM supports UPnP and NAT-PMP for automatic port mapping, opening a listen port in your router enhances incoming peer connections and improves overall swarm speeds.
