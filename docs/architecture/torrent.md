# Feature Guide: BitTorrent Swarm Engine & File Management

## Overview

My-IDM features a high-performance **BitTorrent Swarm Engine** powered by the industry-standard `libtorrent` library. It seamlessly handles `.torrent` files and `magnet:` links alongside standard HTTP/HTTPS downloads in a unified transfer manager.

With advanced multi-peer piece verification, DHT, Peer Exchange (PEX), selective file prioritization, live swarm inspection, and `.fastresume` state caching, My-IDM gives you full control over torrent downloads without requiring a standalone torrent client.

---

## Key Capabilities

### 1. Universal Magnet & Torrent File Support
- **Magnet URIs**: Full support for `magnet:?xt=urn:btih:...` links with embedded trackers and display names.
- **`.torrent` Files**: Add torrent descriptor files via the Add Download dialog, file picker, clipboard monitor, or direct drag-and-drop.
- **Asynchronous Metadata Resolution**: While fetching metadata (`downloading_metadata` state), My-IDM connects to DHT nodes and swarm peers, automatically renaming the task and populating the file tree once metadata is decoded.
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
- **Dedicated Preferences Tab**: BitTorrent-specific settings (seeding behavior, upload limits, ratio, and metadata timeout) are organized in a dedicated **🧲 BitTorrent** tab in the Preferences window (`Ctrl+,` or `Tools -> BitTorrent Settings…`).

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
| **Fastresume Cache** | [`FASTRESUME_DIR`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py#L26) | Directory `~/.my-idm/fastresume/` storing bencoded resume buffers for instant startup. |
| **Priority Controller** | [`set_torrent_file_priority`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Dynamically applies `lt.torrent_handle.file_priority()` across individual files. |
| **Swarm Diagnostics** | [`get_torrent_peers`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) / [`get_torrent_trackers`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Queries `get_peer_info()` and `trackers()` for UI presentation. |
| **Details Panel** | [`my_idm.details_panel.DetailsPanel`](file:///d:/Projects/my-idm/my_idm/details_panel.py) | Multi-tab inspection panel rendering files, peer tables, tracker metrics, and progress bars. |
| **Download Manager** | [`my_idm.manager.DownloadManager`](file:///d:/Projects/my-idm/my_idm/manager.py) | Coordinates download lifecycle, transitions, speed aggregation, and database persistence. |
| **Network Binding** | [`TorrentEngine.apply_network_config`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Sets `outgoing_interfaces` and `listen_interfaces` on `libtorrent.session_settings`. |
| **Tor Routing** | [`TorrentEngine.apply_tor_config`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Configures SOCKS5 proxy and privacy flags on `libtorrent.session_settings`. |
| **State Machine & Lifecycle** | [`docs/architecture/state-machines.md`](file:///d:/Projects/my-idm/docs/architecture/state-machines.md) | Dedicated BitTorrent state diagram, metadata timeout lifecycle, seeding rules, and slot allocation. |

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
