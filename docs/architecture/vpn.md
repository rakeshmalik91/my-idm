# Feature Guide: VPN Interface Binding & Kill Switch Protection

## Overview

My-IDM features robust network interface binding and an automated hardware-level **Kill Switch** designed to ensure complete privacy for sensitive downloads. Rather than relying on generic OS-level routing (which can silently leak packets if a VPN client reconnects or fails), My-IDM binds network sockets directly to the selected network adapter's local IP address and continually monitors its operational state.

---

## Key Capabilities

1. **Strict Interface Binding**:
   - **HTTP Multi-Segment Engine**: Binds every TCP chunk connection to the specific local IP address of the chosen VPN interface (`TCPConnector(local_addr=(interface_ip, 0))`).
   - **BitTorrent Swarm Engine**: Configures `libtorrent` session settings (`listen_interfaces` and `outgoing_interfaces`) exclusively to the VPN IP, preventing peer and tracker leaks across default physical interfaces.

2. **Real-Time Kill Switch**:
   - Background network watcher actively monitors the assigned network adapter status.
   - If the VPN adapter disconnects, loses IP lease, or drops carrier:
     - All active downloads (HTTP segments and BitTorrent swarms) are immediately paused.
     - Status is transitioned to `error` with a clear explanation (`VPN / Bound interface disconnected (Kill switch active)`).
     - Queued downloads and automatic retries are blocked until the adapter reconnects.

3. **Proxy Support (HTTP & SOCKS5)**:
   - Configurable secondary proxy with optional username/password authentication.
   - Routes HTTP downloads via proxy URLs and libtorrent peer/tracker traffic via `lt.proxy_type_t.socks5` or `lt.proxy_type_t.http`.

4. **Live Status Bar Badge**:
   - Clickable badge in the status bar displays adapter name, icon (`🛡️ VPN` vs `🌐 Net`), and Kill Switch state (`[🔒 KS]`).
   - Clicking the badge opens the Network & VPN settings tab directly.

---

## User Interface & Controls

### Opening Network & VPN Settings
- **Shortcut**: Click the status bar network badge (e.g. `🌐 Net: Default` or `🛡️ VPN: wg0 [🔒 KS]`).
- **Menu Bar**: Select **Tools → 🌐 VPN & Network Settings…** or **Tools → ⚙️ Preferences…** (Tab 2: `🌐 Network & VPN`).

### Configuring VPN Binding
1. Check **"Bind downloads to specific network interface"**.
2. Select your VPN interface from the dropdown list.
   - My-IDM automatically detects WireGuard (`wg`), OpenVPN (`tun`, `tap`), Tailscale, NordLynx, and standard adapters.
   - Adapters display their current IPv4 address and adapter type.
3. Click **"🧪 Test Connection"** to verify that the adapter is active and has internet connectivity.

### Configuring Kill Switch
1. Check **"Enable Kill Switch"** under Network Interface Binding.
2. When checked, any disconnection of the selected adapter will instantly freeze all transfers to prevent IP leakage over your unencrypted ISP connection.

### Configuring Proxy
1. In the **Proxy Settings** section, check **"Enable Proxy"**.
2. Select **SOCKS5** or **HTTP**.
3. Enter **Host** (e.g. `127.0.0.1`) and **Port** (e.g. `1080`).
4. (Optional) Provide **Username** and **Password** if authentication is required.
5. Click **"Save Settings"**.

---

## Architecture & Code Reference

| Component | File | Description |
| :--- | :--- | :--- |
| **Config** | [`my_idm.network.NetworkConfig`](file:///d:/Projects/my-idm/my_idm/network.py) | Stores interface name, IP, kill switch boolean, proxy settings, and persistence in `QSettings`. |
| **Detection** | [`my_idm.network.get_available_interfaces`](file:///d:/Projects/my-idm/my_idm/network.py) | Scans local network adapters using `psutil` or platform socket APIs. |
| **Validation** | [`my_idm.network.is_interface_active`](file:///d:/Projects/my-idm/my_idm/network.py) | Verifies carrier status and IP assignment for the active binding. |
| **HTTP Binding** | [`my_idm.http_engine.HTTPEngine._recreate_session`](file:///d:/Projects/my-idm/my_idm/http_engine.py) | Configures `aiohttp.TCPConnector` with `local_addr`. |
| **Torrent Binding** | [`my_idm.torrent_engine.TorrentEngine.apply_network_config`](file:///d:/Projects/my-idm/my_idm/torrent_engine.py) | Configures `listen_interfaces` and `outgoing_interfaces` on `libtorrent.session`. |
| **Kill Switch Loop** | [`my_idm.manager.DownloadManager._process_retry_queue`](file:///d:/Projects/my-idm/my_idm/manager.py) | Checks adapter status before initiating new or retried downloads. |

---

## Troubleshooting

- **Adapter Not In Dropdown**: Ensure your VPN client is actively connected before opening settings, or click the dialog again to refresh interface enumeration.
- **Download Stuck in Error (Kill Switch Active)**: Reconnect your VPN client. Once the interface is restored, right-click the download and select **Resume**.
- **Tor vs VPN**: If both Tor and VPN are enabled, Tor proxy routing takes precedence for privacy encapsulation over the configured network adapter.
