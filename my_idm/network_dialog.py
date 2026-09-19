"""VPN and Network Settings dialog for My-IDM."""

from __future__ import annotations

import asyncio
from typing import Optional

import aiohttp
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from my_idm.network import (
    NetworkConfig,
    NetworkInterfaceInfo,
    get_available_interfaces,
)


class NetworkSettingsDialog(QDialog):
    """Dialog for configuring VPN adapter binding, kill switch, and proxy."""

    def __init__(self, current_config: NetworkConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("VPN & Network Settings")
        self.setMinimumWidth(560)
        self.setModal(True)

        self._config = NetworkConfig.from_dict(current_config.to_dict())
        self._interfaces: list[NetworkInterfaceInfo] = []

        self._setup_ui()
        self._load_interfaces()
        self._populate_fields()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # Header description
        header = QLabel(
            "Configure VPN adapter binding, Kill Switch protection, and optional "
            "proxy servers for all downloads (HTTP & BitTorrent)."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        # Tabs
        tabs = QTabWidget()

        # Tab 1: VPN & Interface Binding
        vpn_tab = QWidget()
        vpn_layout = QVBoxLayout(vpn_tab)
        vpn_layout.setSpacing(14)
        vpn_layout.setContentsMargins(12, 16, 12, 12)

        # Interface selection group
        iface_group = QGroupBox("Network Adapter / VPN Binding")
        iface_inner = QVBoxLayout(iface_group)
        iface_inner.setSpacing(10)

        combo_row = QHBoxLayout()
        self._iface_combo = QComboBox()
        self._iface_combo.currentIndexChanged.connect(self._on_iface_changed)
        combo_row.addWidget(self._iface_combo, 1)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.setToolTip("Rescan local network adapters")
        refresh_btn.clicked.connect(self._load_interfaces)
        combo_row.addWidget(refresh_btn)
        iface_inner.addLayout(combo_row)

        self._iface_details_label = QLabel("")
        self._iface_details_label.setWordWrap(True)
        self._iface_details_label.setStyleSheet("color: #a0aab8; font-size: 11px;")
        iface_inner.addWidget(self._iface_details_label)

        vpn_layout.addWidget(iface_group)

        # Kill Switch group
        ks_group = QGroupBox("VPN Kill Switch")
        ks_inner = QVBoxLayout(ks_group)
        self._kill_switch_cb = QCheckBox("🔒 Enable Kill Switch")
        self._kill_switch_cb.setToolTip(
            "Prevent all downloads and traffic leaks if the VPN or bound adapter disconnects."
        )
        ks_inner.addWidget(self._kill_switch_cb)

        ks_desc = QLabel(
            "When enabled, downloads will halt immediately if the chosen VPN adapter "
            "is not connected or loses its IP address. This prevents any traffic leaks "
            "over your physical internet connection."
        )
        ks_desc.setWordWrap(True)
        ks_desc.setStyleSheet("color: #8892b0; font-size: 11px;")
        ks_inner.addWidget(ks_desc)

        vpn_layout.addWidget(ks_group)
        vpn_layout.addStretch()
        tabs.addTab(vpn_tab, "🛡️ VPN & Adapter Binding")

        # Tab 2: Proxy
        proxy_tab = QWidget()
        proxy_layout = QVBoxLayout(proxy_tab)
        proxy_layout.setSpacing(14)
        proxy_layout.setContentsMargins(12, 16, 12, 12)

        self._proxy_enable_cb = QCheckBox("🌐 Route traffic through proxy server")
        self._proxy_enable_cb.toggled.connect(self._on_proxy_toggled)
        proxy_layout.addWidget(self._proxy_enable_cb)

        proxy_group = QGroupBox("Proxy Server Details")
        proxy_inner = QVBoxLayout(proxy_group)
        proxy_inner.setSpacing(10)

        # Protocol & Port
        type_port_row = QHBoxLayout()
        type_port_row.addWidget(QLabel("Protocol:"))
        self._proxy_type_combo = QComboBox()
        self._proxy_type_combo.addItems(["HTTP", "SOCKS5"])
        type_port_row.addWidget(self._proxy_type_combo, 1)

        type_port_row.addWidget(QLabel("Port:"))
        self._proxy_port_spin = QSpinBox()
        self._proxy_port_spin.setRange(1, 65535)
        self._proxy_port_spin.setValue(8080)
        type_port_row.addWidget(self._proxy_port_spin, 1)
        proxy_inner.addLayout(type_port_row)

        # Host
        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Host / IP:"))
        self._proxy_host_edit = QLineEdit()
        self._proxy_host_edit.setPlaceholderText("e.g., 127.0.0.1 or proxy.example.com")
        host_row.addWidget(self._proxy_host_edit, 1)
        proxy_inner.addLayout(host_row)

        # Auth
        auth_row = QHBoxLayout()
        auth_row.addWidget(QLabel("User (optional):"))
        self._proxy_user_edit = QLineEdit()
        auth_row.addWidget(self._proxy_user_edit, 1)

        auth_row.addWidget(QLabel("Password:"))
        self._proxy_pass_edit = QLineEdit()
        self._proxy_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        auth_row.addWidget(self._proxy_pass_edit, 1)
        proxy_inner.addLayout(auth_row)

        proxy_layout.addWidget(proxy_group)
        proxy_layout.addStretch()
        tabs.addTab(proxy_tab, "🌐 Proxy")

        layout.addWidget(tabs)

        # Bottom row: Test Connection button & Dialog Button Box
        bottom_row = QHBoxLayout()
        test_btn = QPushButton("🧪 Test Connection")
        test_btn.clicked.connect(self._test_connection)
        bottom_row.addWidget(test_btn)
        bottom_row.addStretch()

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_save)
        button_box.rejected.connect(self.reject)
        bottom_row.addWidget(button_box)

        layout.addLayout(bottom_row)

    def _load_interfaces(self):
        """Enumerate interfaces and populate combo box."""
        current_data = self._iface_combo.currentData()
        self._iface_combo.clear()

        # Item 0: Default
        self._iface_combo.addItem("🌐 Any / Default Interface (System Route)", ("", ""))

        self._interfaces = get_available_interfaces()
        selected_idx = 0

        for i, iface in enumerate(self._interfaces, start=1):
            self._iface_combo.addItem(iface.display_name, (iface.name, iface.ip))
            # Check match with current config or previously selected
            target_name = (
                current_data[0] if current_data else self._config.interface_name
            )
            target_ip = (
                current_data[1] if current_data else self._config.interface_ip
            )
            if target_name and iface.name == target_name:
                selected_idx = i
            elif target_ip and iface.ip == target_ip and selected_idx == 0:
                selected_idx = i

        self._iface_combo.setCurrentIndex(selected_idx)
        self._update_details_label()

    def _populate_fields(self):
        """Populate widgets with config values."""
        self._kill_switch_cb.setChecked(self._config.kill_switch)
        self._proxy_enable_cb.setChecked(self._config.proxy_enabled)
        idx = self._proxy_type_combo.findText(self._config.proxy_type.upper())
        if idx >= 0:
            self._proxy_type_combo.setCurrentIndex(idx)
        self._proxy_host_edit.setText(self._config.proxy_host)
        self._proxy_port_spin.setValue(self._config.proxy_port or 8080)
        self._proxy_user_edit.setText(self._config.proxy_username)
        self._proxy_pass_edit.setText(self._config.proxy_password)

        self._on_proxy_toggled(self._config.proxy_enabled)

    def _on_iface_changed(self):
        self._update_details_label()

    def _update_details_label(self):
        data = self._iface_combo.currentData()
        if not data or not data[0]:
            self._iface_details_label.setText(
                "Using the system default routing table. Outgoing traffic uses "
                "whichever network interface Windows selects."
            )
            self._kill_switch_cb.setEnabled(False)
            self._kill_switch_cb.setChecked(False)
        else:
            name, ip = data
            self._iface_details_label.setText(
                f"Traffic is bound to adapter '{name}' with local IP: {ip}. "
                "Only sockets bound to this address will be used for downloads."
            )
            self._kill_switch_cb.setEnabled(True)

    def _on_proxy_toggled(self, checked: bool):
        self._proxy_type_combo.setEnabled(checked)
        self._proxy_port_spin.setEnabled(checked)
        self._proxy_host_edit.setEnabled(checked)
        self._proxy_user_edit.setEnabled(checked)
        self._proxy_pass_edit.setEnabled(checked)

    def _test_connection(self):
        """Test reachability over the selected interface/proxy."""
        data = self._iface_combo.currentData()
        bound_ip = data[1] if data else ""
        proxy_url = ""
        if self._proxy_enable_cb.isChecked() and self._proxy_host_edit.text().strip():
            ptype = self._proxy_type_combo.currentText().lower()
            phost = self._proxy_host_edit.text().strip()
            pport = self._proxy_port_spin.value()
            puser = self._proxy_user_edit.text().strip()
            ppass = self._proxy_pass_edit.text().strip()
            auth = f"{puser}:{ppass}@" if puser and ppass else (f"{puser}@" if puser else "")
            proxy_url = f"{ptype}://{auth}{phost}:{pport}"

        async def _probe():
            connector = None
            if bound_ip:
                connector = aiohttp.TCPConnector(local_addr=(bound_ip, 0))
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                kwargs = {}
                if proxy_url:
                    kwargs["proxy"] = proxy_url
                async with session.get("https://httpbin.org/ip", **kwargs) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        return True, text
                    return False, f"HTTP Status {resp.status}"

        try:
            loop = asyncio.new_event_loop()
            success, message = loop.run_until_complete(_probe())
            loop.close()
            if success:
                QMessageBox.information(
                    self, "Connection Successful",
                    f"Successfully connected to the internet!\n\nResponse:\n{message}"
                )
            else:
                QMessageBox.warning(
                    self, "Connection Failed",
                    f"Test request returned an error:\n{message}"
                )
        except Exception as exc:
            QMessageBox.critical(
                self, "Connection Failed",
                f"Could not connect via the selected configuration:\n{exc}"
            )

    def _on_save(self):
        data = self._iface_combo.currentData()
        if data and data[0]:
            self._config.interface_name = data[0]
            self._config.interface_ip = data[1]
            self._config.kill_switch = self._kill_switch_cb.isChecked()
        else:
            self._config.interface_name = ""
            self._config.interface_ip = ""
            self._config.kill_switch = False

        self._config.proxy_enabled = self._proxy_enable_cb.isChecked()
        self._config.proxy_type = self._proxy_type_combo.currentText().lower()
        self._config.proxy_host = self._proxy_host_edit.text().strip()
        self._config.proxy_port = self._proxy_port_spin.value()
        self._config.proxy_username = self._proxy_user_edit.text().strip()
        self._config.proxy_password = self._proxy_pass_edit.text().strip()

        self.accept()

    @property
    def config(self) -> NetworkConfig:
        return self._config
