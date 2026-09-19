"""Preferences and settings dialog for My-IDM."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl, QSettings
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import asyncio
import aiohttp
from my_idm.config import GeneralConfig, TorConfig, is_tor_reachable, DEFAULT_DOWNLOADS_DIR
from my_idm.database import Database
from my_idm.tor_service import find_tor_executable
from my_idm.network import (
    NetworkConfig,
    NetworkInterfaceInfo,
    get_available_interfaces,
)
from my_idm.security import (
    SecurityConfig,
    find_windows_defender_path,
    scan_file,
)

log = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Preferences / Settings dialog for general downloads, network, Tor, and security."""

    def __init__(
        self,
        general_config: Optional[GeneralConfig] = None,
        network_config: Optional[NetworkConfig] = None,
        security_config: Optional[SecurityConfig] = None,
        tor_config: Optional[TorConfig] = None,
        db: Optional[Database] = None,
        parent=None,
        initial_tab: int = 0,
    ):
        super().__init__(parent)
        self.setWindowTitle("Preferences & Settings")
        self.setMinimumWidth(740)
        self.setMinimumHeight(560)
        self.setModal(True)
        self._db = db

        from my_idm.resources import get_app_icon
        self.setWindowIcon(get_app_icon())

        self._general_cfg = (
            GeneralConfig.from_dict(general_config.to_dict())
            if general_config
            else GeneralConfig.load()
        )
        self._network_cfg = (
            NetworkConfig.from_dict(network_config.to_dict())
            if network_config
            else NetworkConfig.load()
        )
        self._security_cfg = (
            SecurityConfig.from_dict(security_config.to_dict())
            if security_config
            else SecurityConfig.load()
        )
        self._tor_cfg = (
            TorConfig.from_dict(tor_config.to_dict())
            if tor_config
            else TorConfig.load()
        )

        self._interfaces: list[NetworkInterfaceInfo] = []
        self._tabs = QTabWidget()

        self._setup_ui()
        self._populate_fields()
        self._restore_size_from_db()

        if 0 <= initial_tab < self._tabs.count():
            self._tabs.setCurrentIndex(initial_tab)

    def _get_db(self) -> Optional[Database]:
        if self._db is not None:
            return self._db
        parent = self.parent()
        if parent:
            mgr = getattr(parent, "_manager", None)
            if mgr and getattr(mgr, "_db", None):
                self._db = mgr._db
                return self._db
            db = getattr(parent, "_db", None)
            if db:
                self._db = db
                return self._db
        try:
            db = Database()
            db.open()
            self._db = db
            return self._db
        except Exception:
            return None

    def _restore_size_from_db(self):
        """Restore preferences window dimensions from database or QSettings."""
        try:
            db = self._get_db()
            if db:
                size_data = db.get_preferences_window_size()
                if size_data and isinstance(size_data, dict):
                    w = size_data.get("width")
                    h = size_data.get("height")
                    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                        self.resize(max(w, 740), max(h, 560))
                        return
            settings = QSettings("MyIDM", "My-IDM")
            w = settings.value("preferences_dialog_width", type=int)
            h = settings.value("preferences_dialog_height", type=int)
            if w and h and w > 0 and h > 0:
                self.resize(max(w, 740), max(h, 560))
                return
        except Exception as exc:
            log.warning("Failed to restore preferences dialog size from DB: %s", exc)

        # Default widened size (820px width vs original 640px)
        self.resize(820, 600)

    def _save_size_to_db(self):
        """Persist preferences window dimensions to database and QSettings."""
        try:
            w = self.width()
            h = self.height()
            if w > 0 and h > 0:
                db = self._get_db()
                if db:
                    db.save_preferences_window_size(w, h)
                settings = QSettings("MyIDM", "My-IDM")
                settings.setValue("preferences_dialog_width", w)
                settings.setValue("preferences_dialog_height", h)
        except Exception as exc:
            log.warning("Failed to save preferences dialog size to DB: %s", exc)

    def done(self, result: int):
        self._save_size_to_db()
        super().done(result)

    def closeEvent(self, event):
        self._save_size_to_db()
        super().closeEvent(event)

    # -----------------------------------------------------------------------
    # UI Setup
    # -----------------------------------------------------------------------

    def _setup_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setSpacing(14)
        root_layout.setContentsMargins(18, 18, 18, 18)

        # Tabs
        self._tabs.addTab(self._create_general_tab(), "📁 General && Downloads")
        self._tabs.addTab(self._create_network_tab(), "🌐 Network && VPN")
        self._tabs.addTab(self._create_tor_tab(), "🧅 Tor Network")
        self._tabs.addTab(self._create_security_tab(), "🛡️ Antivirus && Security")
        root_layout.addWidget(self._tabs)

        # Dialog Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        self._save_btn = QPushButton("Save Settings")
        self._save_btn.setObjectName("primaryButton")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(self._save_btn)

        root_layout.addLayout(btn_layout)

    def _create_general_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. Default Download Directory
        dir_group = QGroupBox("Default Download Location")
        dir_layout = QVBoxLayout(dir_group)
        dir_layout.setSpacing(10)

        path_row = QHBoxLayout()
        self._save_path_edit = QLineEdit()
        self._save_path_edit.setPlaceholderText(DEFAULT_DOWNLOADS_DIR)
        path_row.addWidget(self._save_path_edit, 1)

        browse_btn = QPushButton("Browse …")
        browse_btn.clicked.connect(self._on_browse_default_path)
        path_row.addWidget(browse_btn)

        open_folder_btn = QPushButton("📁 Open Folder")
        open_folder_btn.setToolTip("Open this download directory in File Explorer")
        open_folder_btn.clicked.connect(self._on_open_default_path)
        path_row.addWidget(open_folder_btn)

        dir_layout.addLayout(path_row)

        self._remember_last_cb = QCheckBox(
            "Remember last used folder when adding downloads"
        )
        self._remember_last_cb.setToolTip(
            "When checked, choosing a different folder in the Add Download dialog "
            "will be automatically used for subsequent downloads."
        )
        dir_layout.addWidget(self._remember_last_cb)

        layout.addWidget(dir_group)

        # 2. Performance & Engine Defaults
        perf_group = QGroupBox("Download Performance && Engine Defaults")
        perf_layout = QVBoxLayout(perf_group)
        perf_layout.setSpacing(10)

        seg_row = QHBoxLayout()
        seg_lbl = QLabel("Default parallel connections (segments) for HTTP:")
        seg_row.addWidget(seg_lbl, 1)
        self._segments_spin = QSpinBox()
        self._segments_spin.setRange(1, 32)
        self._segments_spin.setToolTip("Number of parallel connection streams per HTTP download")
        seg_row.addWidget(self._segments_spin)
        perf_layout.addLayout(seg_row)

        concurrent_row = QHBoxLayout()
        concurrent_lbl = QLabel("Maximum concurrent active downloads:")
        concurrent_row.addWidget(concurrent_lbl, 1)
        self._concurrent_spin = QSpinBox()
        self._concurrent_spin.setRange(1, 20)
        self._concurrent_spin.setToolTip("Maximum number of active downloads transferring simultaneously")
        concurrent_row.addWidget(self._concurrent_spin)
        perf_layout.addLayout(concurrent_row)

        retry_row = QHBoxLayout()
        retry_lbl = QLabel("Maximum automatic retries on connection failure:")
        retry_row.addWidget(retry_lbl, 1)
        self._retries_spin = QSpinBox()
        self._retries_spin.setRange(1, 10)
        self._retries_spin.setToolTip("Number of automatic reconnect attempts before marking as error")
        retry_row.addWidget(self._retries_spin)
        perf_layout.addLayout(retry_row)

        layout.addWidget(perf_group)

        # 3. Application Startup & Notifications
        app_group = QGroupBox("Application Behavior")
        app_layout = QVBoxLayout(app_group)
        app_layout.setSpacing(10)

        self._auto_resume_cb = QCheckBox(
            "Automatically resume incomplete downloads when application starts"
        )
        app_layout.addWidget(self._auto_resume_cb)

        self._notify_cb = QCheckBox(
            "Show desktop / status notification when a download completes"
        )
        app_layout.addWidget(self._notify_cb)

        layout.addWidget(app_group)
        layout.addStretch()
        return tab

    def _create_network_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # VPN Adapter Binding
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

        self._kill_switch_cb = QCheckBox("🔒 Enable Kill Switch")
        self._kill_switch_cb.setToolTip(
            "Prevent all downloads and traffic leaks if the VPN or bound adapter disconnects."
        )
        iface_inner.addWidget(self._kill_switch_cb)

        layout.addWidget(iface_group)

        # Proxy
        proxy_group = QGroupBox("Proxy Server Configuration")
        proxy_inner = QVBoxLayout(proxy_group)
        proxy_inner.setSpacing(10)

        self._proxy_enable_cb = QCheckBox("🌐 Route traffic through proxy server")
        self._proxy_enable_cb.toggled.connect(self._on_proxy_toggled)
        proxy_inner.addWidget(self._proxy_enable_cb)

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

        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Host / IP:"))
        self._proxy_host_edit = QLineEdit()
        self._proxy_host_edit.setPlaceholderText("e.g., 127.0.0.1 or proxy.example.com")
        host_row.addWidget(self._proxy_host_edit, 1)
        proxy_inner.addLayout(host_row)

        auth_row = QHBoxLayout()
        auth_row.addWidget(QLabel("User (opt):"))
        self._proxy_user_edit = QLineEdit()
        auth_row.addWidget(self._proxy_user_edit, 1)

        auth_row.addWidget(QLabel("Password:"))
        self._proxy_pass_edit = QLineEdit()
        self._proxy_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        auth_row.addWidget(self._proxy_pass_edit, 1)
        proxy_inner.addLayout(auth_row)

        layout.addWidget(proxy_group)

        # Test Connection button
        test_btn = QPushButton("🧪 Test Network Connection")
        test_btn.clicked.connect(self._on_test_network)
        layout.addWidget(test_btn)

        layout.addStretch()
        return tab

    def _create_tor_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. Startup & Activation
        startup_group = QGroupBox("Tor Activation & Startup")
        startup_inner = QVBoxLayout(startup_group)
        startup_inner.setSpacing(8)

        self._tor_enable_cb = QCheckBox("🧅 Enable Tor network routing (SOCKS5 proxy)")
        self._tor_enable_cb.setToolTip("Activate Tor proxy routing immediately")
        startup_inner.addWidget(self._tor_enable_cb)

        self._tor_autostart_cb = QCheckBox("🧅 Activate Tor automatically when My-IDM starts")
        startup_inner.addWidget(self._tor_autostart_cb)
        layout.addWidget(startup_group)

        # 2. Traffic Routing
        routing_group = QGroupBox("Traffic Routing Through Tor")
        routing_inner = QVBoxLayout(routing_group)
        routing_inner.setSpacing(8)

        self._tor_route_http_cb = QCheckBox("🌐 Route standard downloads (HTTP / HTTPS) through Tor")
        self._tor_route_http_cb.setToolTip("Route direct HTTP/HTTPS web downloads through Tor SOCKS5 proxy")
        routing_inner.addWidget(self._tor_route_http_cb)

        self._tor_route_torrent_cb = QCheckBox("📦 Route BitTorrent swarms and trackers through Tor")
        self._tor_route_torrent_cb.setToolTip("Route BitTorrent peer and tracker connections through Tor SOCKS5 proxy")
        routing_inner.addWidget(self._tor_route_torrent_cb)
        layout.addWidget(routing_group)

        # 3. SOCKS5 Proxy Configuration
        proxy_group = QGroupBox("Tor SOCKS5 Proxy Settings")
        proxy_inner = QVBoxLayout(proxy_group)
        proxy_inner.setSpacing(10)

        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Host:"))
        self._tor_host_edit = QLineEdit("127.0.0.1")
        self._tor_host_edit.setPlaceholderText("127.0.0.1")
        host_row.addWidget(self._tor_host_edit, 1)

        host_row.addWidget(QLabel("Port:"))
        self._tor_port_spin = QSpinBox()
        self._tor_port_spin.setRange(1, 65535)
        self._tor_port_spin.setValue(9050)
        host_row.addWidget(self._tor_port_spin)
        proxy_inner.addLayout(host_row)

        presets_row = QHBoxLayout()
        presets_row.addWidget(QLabel("Presets:"))
        preset_service_btn = QPushButton("Tor Service (Port 9050)")
        preset_service_btn.clicked.connect(lambda: self._tor_port_spin.setValue(9050))
        preset_browser_btn = QPushButton("Tor Browser (Port 9150)")
        preset_browser_btn.clicked.connect(lambda: self._tor_port_spin.setValue(9150))
        presets_row.addWidget(preset_service_btn)
        presets_row.addWidget(preset_browser_btn)
        presets_row.addStretch()
        proxy_inner.addLayout(presets_row)

        test_row = QHBoxLayout()
        self._tor_test_btn = QPushButton("🧪 Test Tor Connection")
        self._tor_test_btn.clicked.connect(self._on_test_tor)
        test_row.addWidget(self._tor_test_btn)

        self._tor_test_status_lbl = QLabel("")
        test_row.addWidget(self._tor_test_status_lbl, 1)
        proxy_inner.addLayout(test_row)

        layout.addWidget(proxy_group)

        # 4. Optional Executable Path
        exec_group = QGroupBox("Tor Executable (Optional)")
        exec_inner = QVBoxLayout(exec_group)
        path_row = QHBoxLayout()
        self._tor_path_edit = QLineEdit()
        self._tor_path_edit.setPlaceholderText("C:\\Path\\To\\tor.exe (optional)")
        path_row.addWidget(self._tor_path_edit, 1)

        browse_tor_btn = QPushButton("Browse…")
        browse_tor_btn.clicked.connect(self._on_browse_tor_path)
        path_row.addWidget(browse_tor_btn)
        exec_inner.addLayout(path_row)
        layout.addWidget(exec_group)

        layout.addStretch()
        return tab

    def _create_security_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # Pre-Download
        pre_group = QGroupBox("Pre-Download URL & Payload Safety")
        pre_inner = QVBoxLayout(pre_group)
        pre_inner.setSpacing(10)

        self._scan_before_cb = QCheckBox("🔍 Enable safety inspection before downloading starts")
        pre_inner.addWidget(self._scan_before_cb)

        self._warn_ext_cb = QCheckBox(
            "⚠️ Warn when downloading executable or script files (.exe, .msi, .bat, .vbs, .scr, .iso)"
        )
        pre_inner.addWidget(self._warn_ext_cb)

        self._block_dangerous_cb = QCheckBox(
            "🚫 Automatically block high-risk URLs (e.g. deceptive double extensions like file.pdf.exe)"
        )
        pre_inner.addWidget(self._block_dangerous_cb)

        vt_row = QHBoxLayout()
        vt_row.addWidget(QLabel("VirusTotal API Key (opt):"))
        self._vt_key_edit = QLineEdit()
        self._vt_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._vt_key_edit.setPlaceholderText("Paste your VirusTotal API key for cloud intelligence...")
        vt_row.addWidget(self._vt_key_edit, 1)
        pre_inner.addLayout(vt_row)

        layout.addWidget(pre_group)

        # Post-Download
        post_group = QGroupBox("Post-Download Antivirus Scanning")
        post_inner = QVBoxLayout(post_group)
        post_inner.setSpacing(10)

        self._scan_after_cb = QCheckBox("🛡️ Automatically scan completed files with antivirus")
        post_inner.addWidget(self._scan_after_cb)

        self._defender_rb = QRadioButton("Windows Defender (default)")
        post_inner.addWidget(self._defender_rb)

        self._custom_rb = QRadioButton("Custom Antivirus Scanner Executable")
        post_inner.addWidget(self._custom_rb)

        custom_path_row = QHBoxLayout()
        custom_path_row.addWidget(QLabel("Executable:"))
        self._custom_scanner_edit = QLineEdit()
        self._custom_scanner_edit.setPlaceholderText("C:\\Program Files\\...\\scanner.exe")
        custom_path_row.addWidget(self._custom_scanner_edit, 1)
        browse_scanner_btn = QPushButton("Browse…")
        browse_scanner_btn.clicked.connect(self._on_browse_scanner)
        custom_path_row.addWidget(browse_scanner_btn)
        post_inner.addLayout(custom_path_row)

        args_row = QHBoxLayout()
        args_row.addWidget(QLabel("Arguments:"))
        self._custom_args_edit = QLineEdit()
        self._custom_args_edit.setPlaceholderText('"%file%"')
        args_row.addWidget(self._custom_args_edit, 1)
        post_inner.addLayout(args_row)

        layout.addWidget(post_group)

        # Threat Remediation
        action_group = QGroupBox("Action When Threat is Detected")
        action_inner = QVBoxLayout(action_group)
        self._action_warn_rb = QRadioButton("⚠️ Alert user and display threat warning (keep file)")
        action_inner.addWidget(self._action_warn_rb)
        self._action_quarantine_rb = QRadioButton("🗑️ Alert user and automatically quarantine / delete infected file")
        action_inner.addWidget(self._action_quarantine_rb)
        layout.addWidget(action_group)

        # Test Antivirus Scanner button
        test_scanner_btn = QPushButton("🧪 Test Antivirus Scanner")
        test_scanner_btn.clicked.connect(self._on_test_scanner)
        layout.addWidget(test_scanner_btn)

        layout.addStretch()
        return tab

    # -----------------------------------------------------------------------
    # Population & Handlers
    # -----------------------------------------------------------------------

    def _populate_fields(self):
        # General tab
        self._save_path_edit.setText(self._general_cfg.default_save_path)
        self._remember_last_cb.setChecked(self._general_cfg.remember_last_save_path)
        self._segments_spin.setValue(self._general_cfg.default_segments)
        self._concurrent_spin.setValue(self._general_cfg.max_concurrent_downloads)
        self._retries_spin.setValue(self._general_cfg.max_retries)
        self._auto_resume_cb.setChecked(self._general_cfg.auto_resume_startup)
        self._notify_cb.setChecked(self._general_cfg.notify_on_completion)

        # Network tab
        self._load_interfaces()
        self._kill_switch_cb.setChecked(self._network_cfg.kill_switch)
        self._proxy_enable_cb.setChecked(self._network_cfg.proxy_enabled)
        idx = 1 if self._network_cfg.proxy_type == "socks5" else 0
        self._proxy_type_combo.setCurrentIndex(idx)
        self._proxy_port_spin.setValue(self._network_cfg.proxy_port or 8080)
        self._proxy_host_edit.setText(self._network_cfg.proxy_host)
        self._proxy_user_edit.setText(self._network_cfg.proxy_username)
        self._proxy_pass_edit.setText(self._network_cfg.proxy_password)
        self._on_proxy_toggled(self._network_cfg.proxy_enabled)

        # Security tab
        self._scan_before_cb.setChecked(self._security_cfg.scan_before_download)
        self._warn_ext_cb.setChecked(self._security_cfg.warn_high_risk_extensions)
        self._block_dangerous_cb.setChecked(self._security_cfg.block_dangerous_urls)
        self._vt_key_edit.setText(self._security_cfg.virustotal_api_key)

        self._scan_after_cb.setChecked(self._security_cfg.scan_after_download)
        if self._security_cfg.scanner_type == "custom":
            self._custom_rb.setChecked(True)
        else:
            self._defender_rb.setChecked(True)
        self._custom_scanner_edit.setText(self._security_cfg.custom_scanner_path)
        self._custom_args_edit.setText(self._security_cfg.custom_scanner_args or '"%file%"')

        if self._security_cfg.action_on_threat in ("quarantine", "delete"):
            self._action_quarantine_rb.setChecked(True)
        else:
            self._action_warn_rb.setChecked(True)

        # Tor tab
        self._tor_enable_cb.setChecked(self._tor_cfg.enabled)
        self._tor_autostart_cb.setChecked(self._tor_cfg.auto_start_at_startup)
        self._tor_route_http_cb.setChecked(self._tor_cfg.route_http)
        self._tor_route_torrent_cb.setChecked(self._tor_cfg.route_torrent)
        self._tor_host_edit.setText(self._tor_cfg.proxy_host)
        self._tor_port_spin.setValue(self._tor_cfg.proxy_port or 9050)
        detected_tor = self._tor_cfg.tor_executable_path or find_tor_executable() or ""
        self._tor_path_edit.setText(detected_tor)

    def _on_test_tor(self):
        host = self._tor_host_edit.text().strip() or "127.0.0.1"
        port = self._tor_port_spin.value()
        self._tor_test_status_lbl.setText("Testing connection…")
        self._tor_test_status_lbl.setStyleSheet("color: #8be9fd;")
        if is_tor_reachable(host, port):
            self._tor_test_status_lbl.setText(f"✓ Connected to Tor proxy on {host}:{port}")
            self._tor_test_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold;")
        else:
            exe = find_tor_executable(self._tor_path_edit.text().strip())
            if exe:
                self._tor_test_status_lbl.setText(f"Tor service not running. Executable found (will auto-start): {exe}")
                self._tor_test_status_lbl.setStyleSheet("color: #f1fa8c;")
            else:
                self._tor_test_status_lbl.setText(f"✗ Tor proxy not reachable and tor.exe not found")
                self._tor_test_status_lbl.setStyleSheet("color: #ff5555; font-weight: bold;")

    def _on_browse_tor_path(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Tor Executable", "",
            "Executable Files (*.exe);;All Files (*)",
        )
        if path:
            self._tor_path_edit.setText(path)

    def _on_browse_default_path(self):
        cur = self._save_path_edit.text().strip() or DEFAULT_DOWNLOADS_DIR
        path = QFileDialog.getExistingDirectory(self, "Select Default Download Folder", cur)
        if path:
            self._save_path_edit.setText(path)

    def _on_open_default_path(self):
        target = self._save_path_edit.text().strip() or DEFAULT_DOWNLOADS_DIR
        if not os.path.exists(target):
            try:
                os.makedirs(target, exist_ok=True)
            except Exception as ex:
                QMessageBox.warning(self, "Cannot Open Folder", f"Failed to create directory:\n{ex}")
                return
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _load_interfaces(self):
        self._interfaces = get_available_interfaces()
        self._iface_combo.blockSignals(True)
        self._iface_combo.clear()
        self._iface_combo.addItem("🌐 All Interfaces (Default / Automatic)", "")

        selected_idx = 0
        for i, iface in enumerate(self._interfaces, start=1):
            flag = " [VPN]" if iface.is_vpn else ""
            status = "🟢" if iface.is_up else "⚪"
            label = f"{status} {iface.name}{flag} ({iface.ip})"
            self._iface_combo.addItem(label, iface.name)
            if iface.name == self._network_cfg.interface_name:
                selected_idx = i

        self._iface_combo.setCurrentIndex(selected_idx)
        self._iface_combo.blockSignals(False)
        self._on_iface_changed(selected_idx)

    def _on_iface_changed(self, index: int):
        if index <= 0:
            self._iface_details_label.setText("Traffic will use default system routing.")
            return
        iface = self._interfaces[index - 1]
        vpn_txt = "Yes (Virtual/Tunnel Adapter)" if iface.is_vpn else "No (Standard Interface)"
        status_txt = "UP / Active" if iface.is_up else "DOWN / Inactive"
        self._iface_details_label.setText(
            f"IP: {iface.ip} | Status: {status_txt} | VPN: {vpn_txt}"
        )

    def _on_proxy_toggled(self, enabled: bool):
        self._proxy_type_combo.setEnabled(enabled)
        self._proxy_port_spin.setEnabled(enabled)
        self._proxy_host_edit.setEnabled(enabled)
        self._proxy_user_edit.setEnabled(enabled)
        self._proxy_pass_edit.setEnabled(enabled)

    def _on_browse_scanner(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Antivirus Scanner Executable", "",
            "Executables (*.exe);;All Files (*)",
        )
        if path:
            self._custom_scanner_edit.setText(path)
            self._custom_rb.setChecked(True)

    def _on_test_network(self):
        proxy_en = self._proxy_enable_cb.isChecked()
        p_type = self._proxy_type_combo.currentText().lower()
        p_host = self._proxy_host_edit.text().strip()
        p_port = self._proxy_port_spin.value()
        p_user = self._proxy_user_edit.text().strip()
        p_pass = self._proxy_pass_edit.text()

        idx = self._iface_combo.currentIndex()
        bind_ip = self._interfaces[idx - 1].ip if idx > 0 else ""

        proxy_url = ""
        if proxy_en and p_host:
            auth = f"{p_user}:{p_pass}@" if p_user and p_pass else (f"{p_user}@" if p_user else "")
            proxy_url = f"{p_type}://{auth}{p_host}:{p_port}"

        async def _probe():
            connector = None
            if bind_ip:
                connector = aiohttp.TCPConnector(local_addr=(bind_ip, 0))
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
                    f"✅ Successfully connected to the internet!\n\nResponse:\n{message}"
                )
            else:
                QMessageBox.warning(
                    self, "Connection Failed",
                    f"❌ Test request returned an error:\n{message}"
                )
        except Exception as exc:
            QMessageBox.critical(
                self, "Connection Failed",
                f"❌ Could not connect via the selected configuration:\n{exc}"
            )

    def _on_test_scanner(self):
        scanner_type = "custom" if self._custom_rb.isChecked() else "defender"
        custom_path = self._custom_scanner_edit.text().strip()
        custom_args = self._custom_args_edit.text().strip()

        cfg = SecurityConfig(
            scanner_type=scanner_type,
            custom_scanner_path=custom_path,
            custom_scanner_args=custom_args,
        )

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            tf.write(b"Safe test file for antivirus scanning verification.\n")
            temp_path = tf.name

        try:
            res = scan_file(temp_path, cfg)
            if res.is_clean:
                QMessageBox.information(
                    self, "Antivirus Scanner Test",
                    f"✅ Scanner verified successfully!\n\n"
                    f"Scanner: {res.scanner_name}\n"
                    f"Result: Clean (Safe)\n"
                    f"Details: {res.details}",
                )
            else:
                QMessageBox.warning(
                    self, "Antivirus Scanner Test",
                    f"⚠️ Scanner executed but detected a threat or returned non-zero code:\n\n"
                    f"Scanner: {res.scanner_name}\n"
                    f"Result: {res.details}",
                )
        except Exception as ex:
            QMessageBox.critical(
                self, "Antivirus Test Error",
                f"❌ Failed to run scanner:\n\n{ex}",
            )
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    # -----------------------------------------------------------------------
    # Save & Results
    # -----------------------------------------------------------------------

    def _on_save(self):
        # 1. Validate & collect General settings
        save_path = self._save_path_edit.text().strip() or DEFAULT_DOWNLOADS_DIR
        if not os.path.exists(save_path):
            try:
                os.makedirs(save_path, exist_ok=True)
            except Exception as ex:
                QMessageBox.warning(
                    self, "Invalid Folder",
                    f"Could not create download directory:\n{save_path}\n\nError: {ex}",
                )
                return

        self._general_cfg.default_save_path = save_path
        self._general_cfg.last_save_path = save_path
        self._general_cfg.remember_last_save_path = self._remember_last_cb.isChecked()
        self._general_cfg.default_segments = self._segments_spin.value()
        self._general_cfg.max_concurrent_downloads = self._concurrent_spin.value()
        self._general_cfg.max_retries = self._retries_spin.value()
        self._general_cfg.auto_resume_startup = self._auto_resume_cb.isChecked()
        self._general_cfg.notify_on_completion = self._notify_cb.isChecked()
        self._general_cfg.save()

        # 2. Collect Network settings
        idx = self._iface_combo.currentIndex()
        if idx > 0 and idx - 1 < len(self._interfaces):
            self._network_cfg.interface_name = self._interfaces[idx - 1].name
            self._network_cfg.interface_ip = self._interfaces[idx - 1].ip
        else:
            self._network_cfg.interface_name = ""
            self._network_cfg.interface_ip = ""

        self._network_cfg.kill_switch = self._kill_switch_cb.isChecked()
        self._network_cfg.proxy_enabled = self._proxy_enable_cb.isChecked()
        self._network_cfg.proxy_type = self._proxy_type_combo.currentText().lower()
        self._network_cfg.proxy_host = self._proxy_host_edit.text().strip()
        self._network_cfg.proxy_port = self._proxy_port_spin.value()
        self._network_cfg.proxy_username = self._proxy_user_edit.text().strip()
        self._network_cfg.proxy_password = self._proxy_pass_edit.text()
        self._network_cfg.save()

        # 3. Collect Security settings
        self._security_cfg.scan_before_download = self._scan_before_cb.isChecked()
        self._security_cfg.warn_high_risk_extensions = self._warn_ext_cb.isChecked()
        self._security_cfg.block_dangerous_urls = self._block_dangerous_cb.isChecked()
        self._security_cfg.virustotal_api_key = self._vt_key_edit.text().strip()
        self._security_cfg.scan_after_download = self._scan_after_cb.isChecked()
        self._security_cfg.scanner_type = "custom" if self._custom_rb.isChecked() else "defender"
        self._security_cfg.custom_scanner_path = self._custom_scanner_edit.text().strip()
        self._security_cfg.custom_scanner_args = self._custom_args_edit.text().strip()
        self._security_cfg.action_on_threat = (
            "quarantine" if self._action_quarantine_rb.isChecked() else "warn"
        )
        self._security_cfg.save()

        # 4. Collect Tor settings
        self._tor_cfg.enabled = self._tor_enable_cb.isChecked()
        self._tor_cfg.auto_start_at_startup = self._tor_autostart_cb.isChecked()
        self._tor_cfg.route_http = self._tor_route_http_cb.isChecked()
        self._tor_cfg.route_torrent = self._tor_route_torrent_cb.isChecked()
        self._tor_cfg.proxy_host = self._tor_host_edit.text().strip() or "127.0.0.1"
        self._tor_cfg.proxy_port = self._tor_port_spin.value()
        self._tor_cfg.tor_executable_path = self._tor_path_edit.text().strip()
        self._tor_cfg.save()

        self.accept()

    @property
    def general_config(self) -> GeneralConfig:
        return self._general_cfg

    @property
    def network_config(self) -> NetworkConfig:
        return self._network_cfg

    @property
    def security_config(self) -> SecurityConfig:
        return self._security_cfg

    @property
    def tor_config(self) -> TorConfig:
        return self._tor_cfg
