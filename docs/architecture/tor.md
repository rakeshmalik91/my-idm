# Feature Guide: Tor Network Privacy & Traffic Routing

## Overview

My-IDM includes native integration with the **Tor Network** to anonymize downloads and swarm traffic. With a single click on the main toolbar, download traffic is routed through a local Tor SOCKS5 proxy (`127.0.0.1:9050` or `127.0.0.1:9150`).

If Tor is not already running on your system, My-IDM automatically discovers your installed Tor executable (`tor.exe` / Tor Browser), spawns it as a silent background service, verifies the connection, and gracefully stops it when Tor is deactivated or when My-IDM closes.

---

## Key Capabilities

1. **One-Click Toolbar Activation**:
   - Easily toggle Tor privacy on or off directly from the main toolbar (`🧅 Tor: ON` / `🧅 Tor: OFF`).
   - The button tooltip displays active SOCKS5 parameters and currently routed traffic types.

2. **Automated Background Service Lifecycle (`TorServiceManager`)**:
   - **Smart Detection**: If Tor is already running on the configured host and port, My-IDM connects directly without launching a duplicate process.
   - **Silent Background Launch**: If Tor is inactive, My-IDM automatically locates `tor.exe` and launches it with `CREATE_NO_WINDOW` on Windows (no command prompt popup).
   - **Dedicated Data Directory**: Stores isolated session cache and state in `~/.my-idm/tor_data`.
   - **Graceful Teardown**: Automatically shuts down any background Tor process spawned by My-IDM when Tor is turned off or upon application exit.

3. **Auto-Discovery of Tor Executables**:
   - Automatically scans:
     - User-configured custom executable path
     - System `PATH` (`tor`, `tor.exe`)
     - Standard Tor Browser install paths (`C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe`, AppData, etc.)
     - Standard Standalone Tor paths (`C:\Program Files\Tor\tor.exe`)
     - Unix and macOS paths (`/usr/bin/tor`, `/usr/local/bin/tor`, `/opt/homebrew/bin/tor`).

4. **Configurable Traffic Routing**:
   - **Route standard downloads (HTTP / HTTPS)**: Routes chunked HTTP/HTTPS traffic through Tor via `aiohttp-socks` `ProxyConnector`.
   - **Route BitTorrent swarms and trackers**: Directs all BitTorrent peer connections and tracker announcements through the Tor SOCKS5 proxy via `libtorrent` proxy settings (`force_proxy=True`, `proxy_peer_connections=True`, `proxy_tracker_connections=True`).
   - You can enable both or choose to route only HTTP or only BitTorrent traffic.

5. **Configurable Startup Activation**:
   - Enable an option in Preferences to automatically activate Tor every time My-IDM starts.

6. **Interactive Error Alerting**:
   - If Tor executable cannot be found, if the process crashes on launch, or if the SOCKS5 proxy is unreachable within timeout:
     - Tor remains disabled.
     - The toolbar button reverts to unchecked (`🧅 Tor: OFF`).
     - A modal alert dialog (`⚠️ Tor Connection Error`) is shown with detailed diagnostic information.

7. **Status Bar Indicator**:
   - Clickable purple neon indicator in the footer (`🧅 Tor: HTTP, Torrent` or `🧅 Tor: OFF`).
   - Clicking the badge directly opens the **🧅 Tor Network** settings tab.

8. **Live Download Listing Indicator**:
   - Downloads actively transferring through the Tor network display a prominent onion (`🧅`) indicator in the table view across the **Name** and **Status** columns (e.g. `🧅 ubuntu.iso`, `Downloading (Tor 🧅)`).
   - Status text is highlighted in vibrant neon purple (`#bd93f9`).
   - Toggling Tor ON or OFF immediately refreshes all table rows in real-time.
   - Pausing an active transfer or completing it instantly removes the active routing indicator.
   - Sorting by Name continues to order by the underlying file name alphabetically, undisturbed by the icon prefix.

---

## User Interface & Controls

### Toggling Tor On / Off
- Click the **`🧅 Tor: OFF`** button on the toolbar or select **Tools → 🧅 Tor**.
- To view or edit settings, click the status bar Tor badge or select **Tools → 🧅 Tor Network Settings…**.

### Routing an Individual Download Through Tor
Right-click a row → **Route through Tor**. The item is **enabled only while a Tor
SOCKS5 proxy is actually reachable**, and the tooltip says so when it is not.

Availability is a **cached** value. `DownloadManager.tor_available()` performs no I/O; a
background thread probes the SOCKS5 port every few seconds (and immediately after a Tor
start/stop) and publishes the result through `tor_availability_changed`. This matters:
the naive version — probing inline — blocked for the socket timeout, which is **1 s on
Windows**, freezing the GUI on *every* context-menu open and on every row repaint. The
tooltip reads: *"Unavailable: Tor is not running. Start Tor from Tools → Tor, or set its
path in Tools → Tor Network Settings."*

The per-download choice is stored as `metadata["route_through_tor"]`, so it survives restarts and
needs no schema migration. While the download is transferring, its row shows the same 🧅 badge as
globally-routed downloads, and the Name tooltip says the route applies to *this download* rather
than *all downloads*. Toggling it on for an in-flight download restarts that download so the new
route takes effect immediately; the general HTTP proxy is suppressed for it so the two paths
cannot be applied at once.

> **Torrent limitation:** libtorrent exposes proxy settings only at *session* scope, so a
> per-torrent route is recorded but can only take effect the next time that torrent is added to a
> session — it cannot re-route a live torrent in place. HTTP downloads route immediately.

### Configuring Tor Preferences
In **Tools → ⚙️ Preferences…** → **🧅 Tor Network** tab:
1. **Activation & Startup**:
   - Check **"🧅 Enable Tor network routing (SOCKS5 proxy)"** to toggle routing.
   - Check **"🧅 Activate Tor automatically when My-IDM starts"** to ensure Tor is always on upon application launch.
2. **Traffic Routing**:
   - **"Route standard downloads (HTTP / HTTPS) through Tor"**
   - **"Route BitTorrent swarms and trackers through Tor"**
3. **SOCKS5 Proxy Settings**:
   - **Host**: default `127.0.0.1`
   - **Port**: default `9050` (Tor Service) or `9150` (Tor Browser)
   - **Preset Buttons**: Click **"Tor Service (Port 9050)"** or **"Tor Browser (Port 9150)"** to quickly adjust the port.
   - **🧪 Test Tor Connection**: Live socket test that checks connectivity or verifies that your Tor executable is ready to auto-start.
4. **Tor Executable (Optional)**:
   - Displays the detected path to `tor.exe`.
   - Click **"Browse…"** if Tor is installed in a custom location.

---

## Parallel Tor Instances & Tor Browser Coexistence

My-IDM is engineered to operate seamlessly alongside external Tor instances or active Tor Browser sessions without interference or crashes:

1. **Shared Port Connection (Attach Mode)**:
   - When Tor or Tor Browser is already active on the configured port (e.g. `9050` or `9150`), `TorServiceManager` attaches to the running SOCKS5 proxy directly without spawning a duplicate process.
   - **Process Protection**: Because My-IDM did not launch the external Tor process, stopping Tor in My-IDM or quitting the application leaves the external Tor service or Tor Browser running completely untouched.

2. **Parallel Dual-Instance Operation (Different Ports)**:
   - If Tor Browser is running on port `9150` while My-IDM is configured to use port `9050`, My-IDM launches its background Tor instance using its own isolated session directory (`~/.my-idm/tor_data`).
   - Both Tor processes run concurrently in parallel, maintaining independent circuits, consensus data, and SOCKS5 endpoints without port or file lock conflicts.

3. **Port Conflict Resolution**:
   - If another non-Tor process has already bound the configured port (e.g., port `9050`), `TorServiceManager` detects the `Address already in use` error on startup.
   - It gracefully captures the exit code, prevents crashes, leaves active downloads running over direct connections, and alerts the user with an actionable recommendation to switch to port `9150` (or another free port) in Preferences.

4. **Tor Browser Detection**:
   - The connection tester in **Preferences → Tor Network** automatically probes alternative ports. If port `9050` is idle but Tor Browser is active on port `9150`, it alerts the user that Tor Browser was detected and provides a one-click preset button to bind directly to it.

---

## Architecture & Code Reference

| Component | File | Description |
| :--- | :--- | :--- |
| **Config** | [`my_idm.config.TorConfig`](file:///d:/Projects/my-idm/my_idm/config.py) | Manages host, port, routing flags, executable path, and QSettings persistence. |
| **Discovery & Process** | [`my_idm.tor_service.TorServiceManager`](file:///d:/Projects/my-idm/my_idm/tor_service.py) | Auto-discovers `tor.exe`, starts hidden background process, monitors socket, and stops process on exit. |
| **HTTP Routing** | [`my_idm.http_engine.HTTPEngine`](file:///d:/Projects/my-idm/my_idm/http_engine.py) | Connects through `aiohttp_socks.ProxyConnector.from_url(tor_socks_url)`. |
| **Per-Download HTTP Route** | [`my_idm.http_engine.HTTPEngine._session_for_entry`](file:///d:/Projects/my-idm/my_idm/http_engine.py) | Returns a lazily created, dedicated SOCKS5 session for downloads flagged `route_through_tor`, leaving the main session untouched. Closed by `_close_tor_session()` on shutdown and session recreation. |
| **Per-Download Torrent Flag** | [`my_idm.torrent_engine.TorrentEngine.set_torrent_tor_route`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Records the flag; applied at next session add, since libtorrent proxying is session-scoped. |
| **Per-Download API** | [`my_idm.manager.DownloadManager.set_download_tor_route`](file:///d:/Projects/my-idm/my_idm/manager.py) | Validates Tor is reachable, persists `route_through_tor`, and restarts an in-flight download. `tor_available()` is a **cached read**; `refresh_tor_availability()` probes off-thread and emits `tor_availability_changed`. |
| **Badge Logic** | [`my_idm.download_model.DownloadTableModel.is_tor_active_for`](file:///d:/Projects/my-idm/my_idm/download_model.py) | Honours both the global switch and the per-download flag; a flag is only shown as active while Tor is reachable. Availability is injected via `set_tor_availability_provider()`, which points at the manager's cached flag so painting never blocks. |
| **Torrent Routing** | [`my_idm.torrent_engine.TorrentEngine.apply_tor_config`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Sets SOCKS5 proxy on `libtorrent` session with peer & tracker proxying. |
| **UI Integration** | [`my_idm.main_window.MainWindow`](file:///d:/Projects/my-idm/my_idm/main_window.py) | Manages toolbar action, status bar badge, signal handling, modal error alerting, and the per-row **Route through Tor** context-menu item (`_build_download_tor_action`). |

---

## Troubleshooting
- **"Tor executable could not be found"**: If you have Tor Browser installed, ensure it is installed in standard locations (`C:\Program Files\Tor Browser\...`), or use the **Browse…** button in Settings to point to `tor.exe`.
- **Port Conflict (Exit code 1 / Already in use)**: If port 9050 is already occupied by another service, change the port in Settings to `9150` or another free port.
- **Connection Timeout**: If Tor starts but fails to connect within 15 seconds, verify that third-party antivirus or firewall software is not blocking local loopback connections on port 9050.
