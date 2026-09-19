"""Network interface discovery and VPN / proxy configuration for My-IDM."""

from __future__ import annotations

import logging
import socket
from dataclasses import asdict, dataclass
from typing import Optional

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)

# Keywords identifying virtual or VPN network adapters
_VPN_KEYWORDS = (
    "vpn",
    "wireguard",
    "wintun",
    "nord",
    "tap",
    "tun",
    "tailscale",
    "proton",
    "mullvad",
    "surfshark",
    "express",
    "openvpn",
    "zerotier",
    "warp",
    "hamachi",
    "wg",
)


@dataclass
class NetworkInterfaceInfo:
    """Information about a local network interface adapter."""
    name: str
    ip: str = ""
    is_up: bool = True
    is_vpn: bool = False

    @property
    def display_name(self) -> str:
        tag = "🛡️ VPN: " if self.is_vpn else ""
        status = " (UP)" if self.is_up else " (DOWN)"
        ip_str = f" [{self.ip}]" if self.ip else ""
        return f"{tag}{self.name}{ip_str}{status}"


@dataclass
class NetworkConfig:
    """Configuration for network interface binding, VPN kill switch, and proxy."""
    # Interface binding
    interface_name: str = ""  # Empty means system default
    interface_ip: str = ""    # Bound local IP address
    kill_switch: bool = False  # If True, block traffic if interface is down/absent

    # Proxy configuration
    proxy_enabled: bool = False
    proxy_type: str = "http"   # "http" or "socks5"
    proxy_host: str = ""
    proxy_port: int = 8080
    proxy_username: str = ""
    proxy_password: str = ""

    # Bandwidth limits (bytes/second, 0 = unlimited)
    download_limit: int = 0
    upload_limit: int = 0

    @property
    def is_interface_bound(self) -> bool:
        return bool(self.interface_name and self.interface_ip)

    @property
    def proxy_url(self) -> str:
        """Construct proxy URL string for HTTP/HTTPS client requests."""
        if not self.proxy_enabled or not self.proxy_host:
            return ""
        auth = ""
        if self.proxy_username:
            if self.proxy_password:
                auth = f"{self.proxy_username}:{self.proxy_password}@"
            else:
                auth = f"{self.proxy_username}@"
        return f"{self.proxy_type}://{auth}{self.proxy_host}:{self.proxy_port}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> NetworkConfig:
        return cls(
            interface_name=str(data.get("interface_name", "")),
            interface_ip=str(data.get("interface_ip", "")),
            kill_switch=bool(data.get("kill_switch", False)),
            proxy_enabled=bool(data.get("proxy_enabled", False)),
            proxy_type=str(data.get("proxy_type", "http")),
            proxy_host=str(data.get("proxy_host", "")),
            proxy_port=int(data.get("proxy_port", 8080)),
            proxy_username=str(data.get("proxy_username", "")),
            proxy_password=str(data.get("proxy_password", "")),
            download_limit=int(data.get("download_limit", 0)),
            upload_limit=int(data.get("upload_limit", 0)),
        )

    def save(self, settings: Optional[QSettings] = None):
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Network")
        settings.setValue("interface_name", self.interface_name)
        settings.setValue("interface_ip", self.interface_ip)
        settings.setValue("kill_switch", self.kill_switch)
        settings.setValue("proxy_enabled", self.proxy_enabled)
        settings.setValue("proxy_type", self.proxy_type)
        settings.setValue("proxy_host", self.proxy_host)
        settings.setValue("proxy_port", self.proxy_port)
        settings.setValue("proxy_username", self.proxy_username)
        settings.setValue("proxy_password", self.proxy_password)
        settings.setValue("download_limit", self.download_limit)
        settings.setValue("upload_limit", self.upload_limit)
        settings.endGroup()

    @classmethod
    def load(cls, settings: Optional[QSettings] = None) -> NetworkConfig:
        if settings is None:
            settings = QSettings("MyIDM", "My-IDM")
        settings.beginGroup("Network")
        interface_name = settings.value("interface_name", "") or ""
        interface_ip = settings.value("interface_ip", "") or ""
        kill_switch = settings.value("kill_switch", False, type=bool)
        proxy_enabled = settings.value("proxy_enabled", False, type=bool)
        proxy_type = settings.value("proxy_type", "http") or "http"
        proxy_host = settings.value("proxy_host", "") or ""
        proxy_port = settings.value("proxy_port", 8080, type=int)
        proxy_username = settings.value("proxy_username", "") or ""
        proxy_password = settings.value("proxy_password", "") or ""
        download_limit = settings.value("download_limit", 0, type=int)
        upload_limit = settings.value("upload_limit", 0, type=int)
        settings.endGroup()

        return cls(
            interface_name=str(interface_name),
            interface_ip=str(interface_ip),
            kill_switch=bool(kill_switch),
            proxy_enabled=bool(proxy_enabled),
            proxy_type=str(proxy_type),
            proxy_host=str(proxy_host),
            proxy_port=int(proxy_port),
            proxy_username=str(proxy_username),
            proxy_password=str(proxy_password),
            download_limit=int(download_limit or 0),
            upload_limit=int(upload_limit or 0),
        )


def is_vpn_adapter_name(name: str) -> bool:
    """Check if an adapter name suggests a VPN or tunnel."""
    lower = name.lower()
    return any(kw in lower for kw in _VPN_KEYWORDS)


def get_available_interfaces() -> list[NetworkInterfaceInfo]:
    """Enumerate all available network interfaces on the machine.

    Returns a list of NetworkInterfaceInfo sorted with VPNs and active adapters first.
    """
    interfaces: list[NetworkInterfaceInfo] = []

    try:
        import psutil
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()

        for name, snics in addrs.items():
            ipv4 = ""
            for s in snics:
                if s.family == socket.AF_INET and not s.address.startswith("127."):
                    ipv4 = s.address
                    break

            stat = stats.get(name)
            is_up = stat.isup if stat else bool(ipv4)
            is_vpn = is_vpn_adapter_name(name)

            if ipv4:
                interfaces.append(
                    NetworkInterfaceInfo(
                        name=name,
                        ip=ipv4,
                        is_up=is_up,
                        is_vpn=is_vpn,
                    )
                )
    except Exception as exc:
        log.warning("psutil network enumeration failed, falling back to socket: %s", exc)
        # Fallback using stdlib socket
        try:
            hostname = socket.gethostname()
            _, _, ips = socket.gethostbyname_ex(hostname)
            for ip in ips:
                if not ip.startswith("127."):
                    interfaces.append(
                        NetworkInterfaceInfo(
                            name=f"Adapter ({ip})",
                            ip=ip,
                            is_up=True,
                            is_vpn=False,
                        )
                    )
        except Exception as e:
            log.warning("Socket fallback enumeration failed: %s", e)

    # Sort: active VPNs first, then other active adapters, then inactive
    def _sort_key(iface: NetworkInterfaceInfo):
        return (not iface.is_up, not iface.is_vpn, iface.name.lower())

    interfaces.sort(key=_sort_key)
    return interfaces


def is_interface_active(interface_name: str, interface_ip: str) -> bool:
    """Check if a specific interface and IP is currently active and bound."""
    if not interface_name and not interface_ip:
        return True  # Default route

    current_interfaces = get_available_interfaces()
    for iface in current_interfaces:
        if interface_name and iface.name == interface_name:
            if interface_ip:
                return iface.ip == interface_ip and iface.is_up
            return iface.is_up
        if interface_ip and iface.ip == interface_ip:
            return iface.is_up
    return False
