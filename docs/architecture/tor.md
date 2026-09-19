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
   - Downloads actively transferring through the Tor network display a prominent onion (`🧅`) indicator in the table view across the **Name**, **Type**, and **Status** columns (e.g. `🧅 ubuntu.iso`, `🧅 TORRENT`, `Downloading (Tor 🧅)`).
   - Status text is highlighted in vibrant neon purple (`#bd93f9`).
   - Toggling Tor ON or OFF immediately refreshes all table rows in real-time.
   - Pausing an active transfer or completing it instantly removes the active routing indicator.
   - Sorting by Name continues to order by the underlying file name alphabetically, undisturbed by the icon prefix.

---

## User Interface & Controls

### Toggling Tor On / Off
- Click the **`🧅 Tor: OFF`** button on the toolbar or select **Tools → 🧅 Tor**.
- To view or edit settings, click the status bar Tor badge or select **Tools → 🧅 Tor Network Settings…**.

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

## Architecture & Code Reference

| Component | File | Description |
| :--- | :--- | :--- |
| **Config** | [`my_idm.config.TorConfig`](file:///d:/Projects/my-idm/my_idm/config.py) | Manages host, port, routing flags, executable path, and QSettings persistence. |
| **Discovery & Process** | [`my_idm.tor_service.TorServiceManager`](file:///d:/Projects/my-idm/my_idm/tor_service.py) | Auto-discovers `tor.exe`, starts hidden background process, monitors socket, and stops process on exit. |
| **HTTP Routing** | [`my_idm.http_engine.HTTPEngine`](file:///d:/Projects/my-idm/my_idm/http_engine.py) | Connects through `aiohttp_socks.ProxyConnector.from_url(tor_socks_url)`. |
| **Torrent Routing** | [`my_idm.torrent_engine.TorrentEngine.apply_tor_config`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Sets SOCKS5 proxy on `libtorrent` session with peer & tracker proxying. |
| **UI Integration** | [`my_idm.main_window.MainWindow`](file:///d:/Projects/my-idm/my_idm/main_window.py) | Manages toolbar action, status bar badge, signal handling, and modal error alerting. |

---

## Troubleshooting

- **"Tor executable could not be found"**: If you have Tor Browser installed, ensure it is installed in standard locations (`C:\Program Files\Tor Browser\...`), or use the **Browse…** button in Settings to point to `tor.exe`.
- **Port Conflict (Exit code 1 / Already in use)**: If port 9050 is already occupied by another service, change the port in Settings to `9150` or another free port.
- **Connection Timeout**: If Tor starts but fails to connect within 15 seconds, verify that third-party antivirus or firewall software is not blocking local loopback connections on port 9050.
