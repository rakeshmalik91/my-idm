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
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import asyncio
import aiohttp
from my_idm.config import (
    GeneralConfig,
    TorConfig,
    TorrentConfig,
    ExternalToolsConfig,
    BrowserIntegrationConfig,
    is_tor_reachable,
    DEFAULT_DOWNLOADS_DIR,
)
from my_idm.database import Database, APP_DIR
from my_idm.external_tools import launch_animepahe_cli, launch_animepahe_gui, open_file_in_default_app
from my_idm.utils import normalize_path
from my_idm.tor_service import find_tor_executable
from my_idm.network import (
    NetworkConfig,
    NetworkInterfaceInfo,
    get_available_interfaces,
)
from my_idm.security import (
    KNOWN_THREAT_CATEGORIES,
    SecurityConfig,
    find_windows_defender_path,
    scan_file,
)

log = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """Preferences / Settings dialog for general downloads, torrent, network, Tor, security, and browser."""

    def __init__(
        self,
        general_config: Optional[GeneralConfig] = None,
        torrent_config: Optional[TorrentConfig] = None,
        network_config: Optional[NetworkConfig] = None,
        security_config: Optional[SecurityConfig] = None,
        tor_config: Optional[TorConfig] = None,
        external_tools_config: Optional[ExternalToolsConfig] = None,
        browser_config: Optional[BrowserIntegrationConfig] = None,
        db: Optional[Database] = None,
        parent=None,
        initial_tab: int = 0,
        manager=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Preferences & Settings")
        self.setMinimumWidth(740)
        self.setMinimumHeight(560)
        self.setModal(True)
        self._db = db
        self._manager = manager if manager is not None else getattr(parent, "_manager", None)

        from my_idm.resources import get_app_icon
        self.setWindowIcon(get_app_icon())

        self._general_cfg = (
            GeneralConfig.from_dict(general_config.to_dict())
            if general_config
            else GeneralConfig.load()
        )
        self._torrent_cfg = (
            TorrentConfig.from_dict(torrent_config.to_dict())
            if torrent_config
            else TorrentConfig.load()
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
        self._external_tools_cfg = (
            ExternalToolsConfig.from_dict(external_tools_config.to_dict())
            if external_tools_config
            else ExternalToolsConfig.load()
        )
        self._browser_cfg = (
            BrowserIntegrationConfig.from_dict(browser_config.to_dict())
            if browser_config
            else BrowserIntegrationConfig.load()
        )

        self._interfaces: list[NetworkInterfaceInfo] = []
        self._tabs = QTabWidget()

        self._setup_ui()
        if self._manager and hasattr(self._manager, "animepahe_status_changed"):
            self._manager.animepahe_status_changed.connect(self._on_animepahe_status_changed)
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
                elif self._db is not None:
                    # Explicit DB provided with no saved size yet; use default
                    self.resize(820, 600)
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

    @staticmethod
    def _wrap_scrollable(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(content)
        return scroll

    def _setup_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setSpacing(14)
        root_layout.setContentsMargins(18, 18, 18, 18)

        # Tabs
        self._tabs.addTab(self._wrap_scrollable(self._create_general_tab()), "📁 General && Downloads")
        self._tabs.addTab(self._wrap_scrollable(self._create_torrent_tab()), "🧲 BitTorrent")
        self._tabs.addTab(self._wrap_scrollable(self._create_network_tab()), "🌐 Network && VPN")
        self._tabs.addTab(self._wrap_scrollable(self._create_tor_tab()), "🧅 Tor Network")
        self._tabs.addTab(self._wrap_scrollable(self._create_security_tab()), "🛡️ Antivirus && Security")
        self._tabs.addTab(self._wrap_scrollable(self._create_external_tools_tab()), "🛠️ External Tools")
        self._tabs.addTab(self._wrap_scrollable(self._create_browser_tab()), "🌐 Browser Integration")
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

        layout.addWidget(perf_group)

        # 3. Retry Configuration
        retry_group = QGroupBox("Retry Configuration")
        retry_layout = QVBoxLayout(retry_group)
        retry_layout.setSpacing(10)

        retry_row = QHBoxLayout()
        retry_lbl = QLabel("Maximum automatic retries on connection failure:")
        retry_row.addWidget(retry_lbl, 1)
        self._retries_spin = QSpinBox()
        self._retries_spin.setRange(1, 20)
        self._retries_spin.setToolTip("Number of automatic reconnect attempts before marking as error")
        retry_row.addWidget(self._retries_spin)
        retry_layout.addLayout(retry_row)

        self._retry_exp_cb = QCheckBox("📈 Use exponential backoff for connection retries")
        self._retry_exp_cb.setToolTip(
            "When checked, wait time progressively increases between consecutive retry attempts "
            "to reduce server pressure and prevent spamming failed connections."
        )
        self._retry_exp_cb.toggled.connect(self._on_retry_exp_toggled)
        retry_layout.addWidget(self._retry_exp_cb)

        retry_details_layout = QHBoxLayout()
        retry_details_layout.addWidget(QLabel("Initial retry delay:"))
        self._retry_delay_spin = QDoubleSpinBox()
        self._retry_delay_spin.setRange(0.1, 120.0)
        self._retry_delay_spin.setSingleStep(0.5)
        self._retry_delay_spin.setSuffix(" sec")
        self._retry_delay_spin.setToolTip("Initial wait time before the first retry attempt (e.g. 2.0s)")
        retry_details_layout.addWidget(self._retry_delay_spin)

        self._retry_factor_lbl = QLabel("Multiplier:")
        retry_details_layout.addWidget(self._retry_factor_lbl)
        self._retry_factor_spin = QDoubleSpinBox()
        self._retry_factor_spin.setRange(1.0, 10.0)
        self._retry_factor_spin.setSingleStep(0.5)
        self._retry_factor_spin.setSuffix("x")
        self._retry_factor_spin.setToolTip("Factor by which delay multiplies on each retry attempt (e.g. 2.0x -> 2s, 4s, 8s, 16s...)")
        retry_details_layout.addWidget(self._retry_factor_spin)

        self._retry_max_delay_lbl = QLabel("Max cap:")
        retry_details_layout.addWidget(self._retry_max_delay_lbl)
        self._retry_max_delay_spin = QSpinBox()
        self._retry_max_delay_spin.setRange(1, 3600)
        self._retry_max_delay_spin.setSingleStep(10)
        self._retry_max_delay_spin.setSuffix(" sec")
        self._retry_max_delay_spin.setToolTip("Maximum wait time ceiling for retries")
        retry_details_layout.addWidget(self._retry_max_delay_spin)

        retry_layout.addLayout(retry_details_layout)

        layout.addWidget(retry_group)

        # 4. Application Startup & Notifications
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

        # 4. Backlog Auto-Processing Locations
        backlog_group = QGroupBox("Backlog Files Auto-Processing")
        backlog_layout = QVBoxLayout(backlog_group)
        backlog_layout.setSpacing(8)

        backlog_info_lbl = QLabel(
            "Configure folders and files to automatically scan for backlog download URLs on launch.\n"
            "By default, My-IDM scans project directory, application directory, and user home."
        )
        backlog_info_lbl.setWordWrap(True)
        backlog_info_lbl.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        backlog_layout.addWidget(backlog_info_lbl)

        self._backlog_list = QListWidget()
        self._backlog_list.setMaximumHeight(120)
        backlog_layout.addWidget(self._backlog_list)

        btn_row = QHBoxLayout()
        add_folder_btn = QPushButton("📁 Add Folder…")
        add_folder_btn.clicked.connect(self._on_add_backlog_folder)
        btn_row.addWidget(add_folder_btn)

        add_file_btn = QPushButton("📄 Add File…")
        add_file_btn.clicked.connect(self._on_add_backlog_file)
        btn_row.addWidget(add_file_btn)

        remove_btn = QPushButton("🗑 Remove")
        remove_btn.clicked.connect(self._on_remove_backlog_loc)
        btn_row.addWidget(remove_btn)

        reset_btn = QPushButton("↺ Reset Defaults")
        reset_btn.clicked.connect(self._on_reset_backlog_defaults)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()

        backlog_layout.addLayout(btn_row)

        self._clear_backlog_cb = QCheckBox(
            "Clear entries from backlog file after processing successfully"
        )
        self._clear_backlog_cb.setToolTip(
            "When checked, URLs that are successfully queued, resumed, or already in progress "
            "are removed from the backlog file to prevent duplicate processing on subsequent runs."
        )
        backlog_layout.addWidget(self._clear_backlog_cb)

        poll_row = QHBoxLayout()
        self._backlog_poll_cb = QCheckBox("Periodically scan for new backlog entries")
        self._backlog_poll_cb.setToolTip(
            "When checked, My-IDM automatically scans configured backlog folders and files "
            "for new downloads at regular intervals."
        )
        poll_row.addWidget(self._backlog_poll_cb)

        poll_lbl = QLabel("Interval:")
        poll_row.addWidget(poll_lbl)

        self._backlog_poll_spin = QSpinBox()
        self._backlog_poll_spin.setRange(5, 3600)
        self._backlog_poll_spin.setSingleStep(15)
        self._backlog_poll_spin.setSuffix(" sec")
        self._backlog_poll_spin.setToolTip("Polling frequency in seconds (default: 60s / 1 min)")
        poll_row.addWidget(self._backlog_poll_spin)
        poll_row.addStretch()

        backlog_layout.addLayout(poll_row)

        self._backlog_poll_cb.toggled.connect(self._backlog_poll_spin.setEnabled)

        layout.addWidget(backlog_group)
        layout.addStretch()
        return tab

    def _create_torrent_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. Seeding & State Configuration
        seeding_group = QGroupBox("BitTorrent Seeding && State")
        seeding_layout = QVBoxLayout(seeding_group)
        seeding_layout.setSpacing(10)

        self._seeding_after_complete_cb = QCheckBox(
            "🌱 Continue seeding torrent after download finishes"
        )
        self._seeding_after_complete_cb.setToolTip(
            "When checked, completed torrents automatically transition into the 'seeding' state "
            "rather than stopping immediately."
        )
        seeding_layout.addWidget(self._seeding_after_complete_cb)

        self._resume_seeding_cb = QCheckBox(
            "🔄 Resume seeding torrents on startup"
        )
        self._resume_seeding_cb.setToolTip(
            "When checked, torrents that were in the 'seeding' status when My-IDM was closed "
            "will automatically resume seeding upon startup."
        )
        seeding_layout.addWidget(self._resume_seeding_cb)

        time_row = QHBoxLayout()
        time_lbl = QLabel("Maximum seeding duration:")
        time_row.addWidget(time_lbl, 1)
        self._seeding_time_spin = QSpinBox()
        self._seeding_time_spin.setRange(0, 525_600)  # Up to 1 year in minutes
        self._seeding_time_spin.setSingleStep(15)
        self._seeding_time_spin.setSuffix(" min")
        self._seeding_time_spin.setSpecialValueText("Unlimited (Indefinite)")
        self._seeding_time_spin.setToolTip(
            "Automatically stop seeding after the torrent has been seeding for this many minutes.\n"
            "Set to 0 to seed indefinitely."
        )
        time_row.addWidget(self._seeding_time_spin)
        seeding_layout.addLayout(time_row)

        ratio_limit_row = QHBoxLayout()
        ratio_limit_lbl = QLabel("Maximum share ratio limit:")
        ratio_limit_row.addWidget(ratio_limit_lbl, 1)
        self._seeding_ratio_limit_spin = QDoubleSpinBox()
        self._seeding_ratio_limit_spin.setRange(0.0, 100.0)
        self._seeding_ratio_limit_spin.setSingleStep(0.1)
        self._seeding_ratio_limit_spin.setSuffix(" x")
        self._seeding_ratio_limit_spin.setSpecialValueText("Unlimited (0.0x)")
        self._seeding_ratio_limit_spin.setToolTip(
            "Automatically stop seeding when the upload to download share ratio reaches this limit.\n"
            "Set to 0.0 for unlimited share ratio."
        )
        ratio_limit_row.addWidget(self._seeding_ratio_limit_spin)
        seeding_layout.addLayout(ratio_limit_row)

        speed_row = QHBoxLayout()
        speed_lbl = QLabel("Maximum upload / seeding speed limit:")
        speed_row.addWidget(speed_lbl, 1)
        self._max_seeding_speed_spin = QSpinBox()
        self._max_seeding_speed_spin.setRange(0, 10_000_000)
        self._max_seeding_speed_spin.setSingleStep(10)
        self._max_seeding_speed_spin.setSuffix(" KB/s")
        self._max_seeding_speed_spin.setSpecialValueText("Unlimited (0 KB/s)")
        self._max_seeding_speed_spin.setToolTip(
            "Cap the seeding upload speed in KB/s. Set to 0 for unlimited speed."
        )
        speed_row.addWidget(self._max_seeding_speed_spin)
        seeding_layout.addLayout(speed_row)

        ratio_row = QHBoxLayout()
        ratio_lbl = QLabel("Download to seeding speed ratio:")
        ratio_row.addWidget(ratio_lbl, 1)
        self._seeding_ratio_spin = QDoubleSpinBox()
        self._seeding_ratio_spin.setRange(0.1, 100.0)
        self._seeding_ratio_spin.setSingleStep(0.5)
        self._seeding_ratio_spin.setSuffix(" : 1")
        self._seeding_ratio_spin.setToolTip(
            "Ratio of download speed to seeding speed (e.g. 10.0 = 10:1 ratio, seeding is 10% of download speed).\n"
            "When a global download limit is configured, seeding upload limit is derived as:\n"
            "download limit / ratio."
        )
        ratio_row.addWidget(self._seeding_ratio_spin)
        seeding_layout.addLayout(ratio_row)

        layout.addWidget(seeding_group)

        # 2. Metadata Fetching & Timeouts
        meta_group = QGroupBox("Metadata Fetching && Timeouts")
        meta_layout = QVBoxLayout(meta_group)
        meta_layout.setSpacing(10)

        meta_row = QHBoxLayout()
        meta_lbl = QLabel("Auto-suspend BitTorrent after stuck in metadata fetch:")
        meta_row.addWidget(meta_lbl, 1)
        self._metadata_timeout_spin = QSpinBox()
        self._metadata_timeout_spin.setRange(0, 365)
        self._metadata_timeout_spin.setSingleStep(1)
        self._metadata_timeout_spin.setSuffix(" day(s)")
        self._metadata_timeout_spin.setToolTip(
            "If a BitTorrent magnet link stays stuck fetching metadata longer than "
            "this many days, it is automatically suspended. Set to 0 to disable."
        )
        meta_row.addWidget(self._metadata_timeout_spin)
        meta_layout.addLayout(meta_row)

        layout.addWidget(meta_group)
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
        startup_group = QGroupBox("Tor Activation && Startup")
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
        pre_group = QGroupBox("Pre-Download URL && Payload Safety")
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

        # Scan Timing
        timing_group = QGroupBox("Scan Timing")
        timing_inner = QVBoxLayout(timing_group)
        timing_inner.setSpacing(8)

        self._timing_auto_rb = QRadioButton("🔄 Automatically scan when download completes")
        timing_inner.addWidget(self._timing_auto_rb)

        self._timing_manual_rb = QRadioButton("🖱️ Manual scan only (right-click → Scan with Antivirus)")
        timing_inner.addWidget(self._timing_manual_rb)

        layout.addWidget(timing_group)

        # Threat Remediation
        action_group = QGroupBox("Action When Threat is Detected")
        action_inner = QVBoxLayout(action_group)
        self._action_warn_rb = QRadioButton("⚠️ Alert user and display threat warning (keep file)")
        action_inner.addWidget(self._action_warn_rb)
        self._action_quarantine_rb = QRadioButton("🗑️ Alert user and automatically quarantine / delete infected file")
        action_inner.addWidget(self._action_quarantine_rb)
        layout.addWidget(action_group)

        # Threat Exclusions
        excl_group = QGroupBox("Threat Exclusions (silently allowed)")
        excl_inner = QVBoxLayout(excl_group)
        excl_inner.setSpacing(8)

        excl_desc = QLabel(
            "Threats matching any pattern or category in this list (e.g. HackTool, CrackTool, Keygen) "
            "will be silently allowed without triggering warnings or quarantine actions."
        )
        excl_desc.setWordWrap(True)
        excl_desc.setStyleSheet("color: #a0aab8; font-size: 11px;")
        excl_inner.addWidget(excl_desc)

        self._threat_excl_list = QListWidget()
        self._threat_excl_list.setMaximumHeight(130)
        excl_inner.addWidget(self._threat_excl_list)

        add_row = QHBoxLayout()
        self._new_threat_excl_edit = QLineEdit()
        self._new_threat_excl_edit.setPlaceholderText("Enter threat category or pattern (e.g. Win32/Keygen, CrackTool, PUA)")
        self._new_threat_excl_edit.returnPressed.connect(self._on_add_threat_exclusion)
        add_row.addWidget(self._new_threat_excl_edit, 1)

        add_btn = QPushButton("➕ Add")
        add_btn.clicked.connect(self._on_add_threat_exclusion)
        add_row.addWidget(add_btn)

        remove_btn = QPushButton("🗑 Remove")
        remove_btn.clicked.connect(self._on_remove_threat_exclusion)
        add_row.addWidget(remove_btn)

        reset_btn = QPushButton("↺ Reset Defaults")
        reset_btn.clicked.connect(self._on_reset_threat_exclusions_defaults)
        add_row.addWidget(reset_btn)

        excl_inner.addLayout(add_row)
        layout.addWidget(excl_group)

        # Test Antivirus Scanner button
        test_scanner_btn = QPushButton("🧪 Test Antivirus Scanner")
        test_scanner_btn.clicked.connect(self._on_test_scanner)
        layout.addWidget(test_scanner_btn)

        layout.addStretch()
        return tab

    def _create_external_tools_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # AnimePahe Scraper Group
        ap_group = QGroupBox("AnimePahe Auto-Downloader / Scraper")
        ap_layout = QVBoxLayout(ap_group)
        ap_layout.setSpacing(12)

        # 1. Repository Location
        repo_lbl = QLabel("Repository Location:")
        ap_layout.addWidget(repo_lbl)

        repo_row = QHBoxLayout()
        self._animepahe_repo_edit = QLineEdit()
        self._animepahe_repo_edit.setPlaceholderText(r"e.g. D:\Projects\animepahe-downloader")
        repo_row.addWidget(self._animepahe_repo_edit, 1)

        browse_btn = QPushButton("Browse …")
        browse_btn.clicked.connect(self._on_browse_animepahe_repo)
        repo_row.addWidget(browse_btn)

        open_folder_btn = QPushButton("📁 Open Folder")
        open_folder_btn.setToolTip("Open AnimePahe repository directory in File Explorer")
        open_folder_btn.clicked.connect(self._on_open_animepahe_folder)
        repo_row.addWidget(open_folder_btn)

        ap_layout.addLayout(repo_row)

        # 2. Startup Option
        self._animepahe_startup_cb = QCheckBox(
            "Launch AnimePahe scraper on startup (CLI mode, forwards downloads to My-IDM backlog)"
        )
        self._animepahe_startup_cb.setToolTip(
            "When enabled, My-IDM automatically runs animepahe_download.py in CLI mode at startup.\n"
            "Discovered episodes are sent directly to the My-IDM backlog file for automatic downloading."
        )
        ap_layout.addWidget(self._animepahe_startup_cb)

        desc_lbl = QLabel(
            "ℹ️ In CLI mode, the scraper performs an automated library check in the background. "
            "All new episodes will be queued into the backlog file and ingested automatically."
        )
        desc_lbl.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        desc_lbl.setWordWrap(True)
        ap_layout.addWidget(desc_lbl)

        # 3. Logs & Actions Group
        logs_group = QGroupBox("Diagnostics && Logs")
        logs_layout = QVBoxLayout(logs_group)
        logs_layout.setSpacing(8)

        log_btns_layout = QHBoxLayout()

        self._btn_view_console_log = QPushButton("📄 View Console Logs")
        self._btn_view_console_log.setToolTip("Open CLI stdout/stderr redirection log file")
        self._btn_view_console_log.clicked.connect(self._on_view_animepahe_console_log)
        log_btns_layout.addWidget(self._btn_view_console_log)

        self._btn_view_debug_log = QPushButton("🔍 View Debug Logs")
        self._btn_view_debug_log.setToolTip("Open AnimePahe debug_log.txt")
        self._btn_view_debug_log.clicked.connect(self._on_view_animepahe_debug_log)
        log_btns_layout.addWidget(self._btn_view_debug_log)

        self._btn_run_cli_now = QPushButton("▶️ Run CLI Now")
        self._btn_run_cli_now.setToolTip("Launch AnimePahe background scraper in CLI mode")
        self._btn_run_cli_now.clicked.connect(self._on_run_animepahe_cli_from_settings)
        log_btns_layout.addWidget(self._btn_run_cli_now)

        self._btn_launch_gui_now = QPushButton("🎬 Launch GUI Now")
        self._btn_launch_gui_now.setToolTip("Launch AnimePahe standalone desktop interface")
        self._btn_launch_gui_now.clicked.connect(self._on_launch_animepahe_gui_from_settings)
        log_btns_layout.addWidget(self._btn_launch_gui_now)

        logs_layout.addLayout(log_btns_layout)
        ap_layout.addWidget(logs_group)

        layout.addWidget(ap_group)
        layout.addStretch()
        return tab

    def _create_browser_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. Server Configuration Group
        server_group = QGroupBox("Local Extension Loopback Server")
        server_layout = QVBoxLayout(server_group)
        server_layout.setSpacing(10)

        self._browser_enabled_cb = QCheckBox("Enable Browser Integration (starts HTTP loopback listener)")
        self._browser_enabled_cb.setToolTip("Enables the local REST server that receives downloads from the Chrome extension.")
        server_layout.addWidget(self._browser_enabled_cb)

        port_row = QHBoxLayout()
        port_lbl = QLabel("Server Port:")
        port_row.addWidget(port_lbl)

        self._browser_port_spin = QSpinBox()
        self._browser_port_spin.setRange(1024, 65535)
        self._browser_port_spin.setValue(self._browser_cfg.port or 19582)
        port_row.addWidget(self._browser_port_spin)

        self._browser_status_lbl = QLabel("Checking status...")
        self._browser_status_lbl.setStyleSheet("color: #8fa0b5; margin-left: 12px;")
        port_row.addWidget(self._browser_status_lbl, 1)
        server_layout.addLayout(port_row)

        self._browser_intercept_cb = QCheckBox("Automatically intercept downloads from Chrome")
        self._browser_intercept_cb.setToolTip("When enabled, browser downloads are cancelled in Chrome and handed to My-IDM.")
        server_layout.addWidget(self._browser_intercept_cb)

        bypass_lbl = QLabel("Bypassed File Extensions (comma-separated):")
        server_layout.addWidget(bypass_lbl)

        self._browser_bypass_edit = QLineEdit()
        self._browser_bypass_edit.setPlaceholderText(".torrent, .crx, .pdf")
        self._browser_bypass_edit.setText(", ".join(self._browser_cfg.bypassed_extensions))
        server_layout.addWidget(self._browser_bypass_edit)

        layout.addWidget(server_group)

        # 2. Chrome Extension Setup Group
        setup_group = QGroupBox("Chrome / Brave / Edge Setup Instructions")
        setup_layout = QVBoxLayout(setup_group)
        setup_layout.setSpacing(10)

        instructions = (
            "<p style='line-height: 1.6; margin-bottom: 8px;'>"
            "<b>How to install the local extension in Chrome / Brave / Edge:</b><br>"
            "1. Open your browser and navigate to: <code style='color: #00d2ff;'>chrome://extensions/</code><br>"
            "2. Turn <b>ON</b> the <b>Developer mode</b> toggle switch in the top-right corner.<br>"
            "3. Click the <b>Load unpacked</b> button in the top-left corner.<br>"
            "4. Select the <b>browser_extension</b> folder located inside your My-IDM directory."
            "</p>"
        )
        instr_lbl = QLabel(instructions)
        instr_lbl.setTextFormat(Qt.TextFormat.RichText)
        instr_lbl.setWordWrap(True)
        setup_layout.addWidget(instr_lbl)

        btn_row = QHBoxLayout()

        self._btn_open_ext_folder = QPushButton("📁 Open Extension Folder")
        self._btn_open_ext_folder.setToolTip("Open the browser_extension folder in Windows File Explorer")
        self._btn_open_ext_folder.clicked.connect(self._on_open_extension_folder)
        btn_row.addWidget(self._btn_open_ext_folder)

        self._btn_copy_ext_path = QPushButton("📋 Copy Folder Path")
        self._btn_copy_ext_path.setToolTip("Copy absolute path of the extension directory to clipboard")
        self._btn_copy_ext_path.clicked.connect(self._on_copy_extension_path)
        btn_row.addWidget(self._btn_copy_ext_path)

        self._btn_open_chrome_extensions = QPushButton("🌐 Open chrome://extensions")
        self._btn_open_chrome_extensions.setToolTip("Open Chrome Extensions management page in your browser")
        self._btn_open_chrome_extensions.clicked.connect(self._on_open_chrome_extensions)
        btn_row.addWidget(self._btn_open_chrome_extensions)

        setup_layout.addLayout(btn_row)
        layout.addWidget(setup_group)

        layout.addStretch()
        return tab

    def _get_extension_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "browser_extension"

    def _on_open_extension_folder(self):
        ext_dir = self._get_extension_dir()
        if ext_dir.is_dir():
            open_file_in_default_app(ext_dir)
        else:
            QMessageBox.warning(self, "Folder Not Found", f"Extension folder does not exist:\n{ext_dir}")

    def _on_copy_extension_path(self):
        from PySide6.QtWidgets import QApplication
        ext_dir = self._get_extension_dir()
        QApplication.clipboard().setText(str(ext_dir))
        QMessageBox.information(self, "Path Copied", f"Extension folder path copied to clipboard:\n{ext_dir}")

    def _on_open_chrome_extensions(self):
        try:
            QDesktopServices.openUrl(QUrl("chrome://extensions/"))
        except Exception:
            pass

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
        self._retry_exp_cb.setChecked(self._general_cfg.retry_exponential_backoff)
        self._retry_delay_spin.setValue(self._general_cfg.retry_delay)
        self._retry_factor_spin.setValue(self._general_cfg.retry_backoff_factor)
        self._retry_max_delay_spin.setValue(int(self._general_cfg.retry_max_delay))
        self._on_retry_exp_toggled(self._general_cfg.retry_exponential_backoff)
        self._auto_resume_cb.setChecked(self._general_cfg.auto_resume_startup)
        self._notify_cb.setChecked(self._general_cfg.notify_on_completion)

        # BitTorrent tab
        self._seeding_after_complete_cb.setChecked(self._torrent_cfg.seeding_after_complete)
        self._resume_seeding_cb.setChecked(self._torrent_cfg.resume_seeding_on_startup)
        self._seeding_time_spin.setValue(self._torrent_cfg.seeding_time_limit_minutes)
        self._seeding_ratio_limit_spin.setValue(self._torrent_cfg.seeding_ratio_limit)
        self._max_seeding_speed_spin.setValue(self._torrent_cfg.max_seeding_speed)
        self._seeding_ratio_spin.setValue(self._torrent_cfg.download_to_seeding_ratio)
        self._metadata_timeout_spin.setValue(self._torrent_cfg.metadata_fetch_timeout_days)

        # Backlog locations
        self._backlog_list.clear()
        for loc in self._general_cfg.get_effective_backlog_locations():
            self._backlog_list.addItem(loc)
        self._clear_backlog_cb.setChecked(self._general_cfg.clear_backlog_after_load)
        self._backlog_poll_cb.setChecked(self._general_cfg.backlog_poll_enabled)
        self._backlog_poll_spin.setValue(self._general_cfg.backlog_poll_interval)
        self._backlog_poll_spin.setEnabled(self._general_cfg.backlog_poll_enabled)

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

        # Scan timing
        if self._security_cfg.scan_timing == "manual_only":
            self._timing_manual_rb.setChecked(True)
        else:
            self._timing_auto_rb.setChecked(True)

        # Threat exclusions list
        self._threat_excl_list.clear()
        for cat in self._security_cfg.get_effective_threat_exclusions():
            if cat.strip():
                self._threat_excl_list.addItem(cat.strip())
        if self._security_cfg.ignored_threat_patterns:
            patterns = self._security_cfg.ignored_threat_patterns
            if isinstance(patterns, str):
                patterns = [p.strip() for p in patterns.split(",") if p.strip()]
            for pat in patterns:
                existing = [
                    self._threat_excl_list.item(i).text().strip().lower()
                    for i in range(self._threat_excl_list.count())
                ]
                if pat.lower() not in existing:
                    self._threat_excl_list.addItem(pat)

        # Tor tab
        self._tor_enable_cb.setChecked(self._tor_cfg.enabled)
        self._tor_autostart_cb.setChecked(self._tor_cfg.auto_start_at_startup)
        self._tor_route_http_cb.setChecked(self._tor_cfg.route_http)
        self._tor_route_torrent_cb.setChecked(self._tor_cfg.route_torrent)
        self._tor_host_edit.setText(self._tor_cfg.proxy_host)
        self._tor_port_spin.setValue(self._tor_cfg.proxy_port or 9050)
        detected_tor = self._tor_cfg.tor_executable_path or find_tor_executable() or ""
        self._tor_path_edit.setText(detected_tor)

        # External Tools tab
        self._animepahe_repo_edit.setText(self._external_tools_cfg.animepahe_repo_path)
        self._animepahe_startup_cb.setChecked(self._external_tools_cfg.animepahe_launch_on_startup)
        if self._manager and hasattr(self._manager, "is_animepahe_running") and self._manager.is_animepahe_running():
            self._btn_run_cli_now.setText("⏹️ Stop CLI Scraper")
            self._btn_run_cli_now.setToolTip("Stop running AnimePahe background scraper")
        else:
            self._btn_run_cli_now.setText("▶️ Run CLI Now")
            self._btn_run_cli_now.setToolTip("Launch AnimePahe background scraper in CLI mode")

        # Browser Integration tab
        self._browser_enabled_cb.setChecked(self._browser_cfg.enabled)
        self._browser_port_spin.setValue(self._browser_cfg.port or 19582)
        self._browser_intercept_cb.setChecked(self._browser_cfg.intercept_all)
        self._browser_bypass_edit.setText(", ".join(self._browser_cfg.bypassed_extensions))
        if self._manager and getattr(self._manager, "browser_server", None) and self._manager.browser_server.is_running:
            self._browser_status_lbl.setText(f"🟢 Active (Listening on http://127.0.0.1:{self._browser_cfg.port})")
            self._browser_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold; margin-left: 12px;")
        elif self._browser_cfg.enabled:
            self._browser_status_lbl.setText("⚪ Server will start on apply")
            self._browser_status_lbl.setStyleSheet("color: #f1fa8c; margin-left: 12px;")
        else:
            self._browser_status_lbl.setText("⚪ Disabled")
            self._browser_status_lbl.setStyleSheet("color: #8fa0b5; margin-left: 12px;")

    def _on_test_tor(self):
        host = self._tor_host_edit.text().strip() or "127.0.0.1"
        port = self._tor_port_spin.value()
        self._tor_test_status_lbl.setText("Testing connection…")
        self._tor_test_status_lbl.setStyleSheet("color: #8be9fd;")
        if is_tor_reachable(host, port):
            self._tor_test_status_lbl.setText(f"✓ Connected to Tor proxy on {host}:{port}")
            self._tor_test_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold;")
        else:
            other_port = 9150 if port == 9050 else 9050
            other_desc = "Tor Browser" if other_port == 9150 else "Tor Service"
            if is_tor_reachable(host, other_port):
                self._tor_test_status_lbl.setText(
                    f"Port {port} not running, but {other_desc} is active on port {other_port}! Click '{other_desc} (Port {other_port})' above to use it."
                )
                self._tor_test_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold;")
            else:
                exe = find_tor_executable(self._tor_path_edit.text().strip())
                if exe:
                    self._tor_test_status_lbl.setText(f"Tor service not running. Executable found (will auto-start): {exe}")
                    self._tor_test_status_lbl.setStyleSheet("color: #f1fa8c;")
                else:
                    self._tor_test_status_lbl.setText(f"✗ Tor proxy not reachable and tor.exe not found")
                    self._tor_test_status_lbl.setStyleSheet("color: #ff5555; font-weight: bold;")

    def _on_browse_animepahe_repo(self):
        cur = self._animepahe_repo_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Select AnimePahe Repository Directory", cur)
        if path:
            self._animepahe_repo_edit.setText(normalize_path(path))

    def _on_open_animepahe_folder(self):
        target = self._animepahe_repo_edit.text().strip()
        if not target or not os.path.isdir(target):
            QMessageBox.warning(self, "Folder Not Found", f"The directory does not exist:\n{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _on_view_animepahe_console_log(self):
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        log_path = self._external_tools_cfg.get_console_log_path()
        ok, msg = open_file_in_default_app(log_path, create_if_missing=True)
        if not ok:
            QMessageBox.warning(self, "Cannot Open Console Log", msg)

    def _on_view_animepahe_debug_log(self):
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        log_path = self._external_tools_cfg.get_debug_log_path()
        ok, msg = open_file_in_default_app(log_path, create_if_missing=True)
        if not ok:
            QMessageBox.warning(self, "Cannot Open Debug Log", msg)

    def _on_launch_animepahe_gui_from_settings(self):
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        ok, msg = launch_animepahe_gui(self._external_tools_cfg)
        if ok:
            QMessageBox.information(self, "AnimePahe GUI", msg)
        else:
            QMessageBox.warning(self, "Launch Failed", msg)

    def _on_run_animepahe_cli_from_settings(self):
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        repo = self._external_tools_cfg.get_effective_repo_path()
        if not repo or not os.path.isdir(repo):
            QMessageBox.warning(
                self,
                "Repository Not Found",
                f"AnimePahe repository directory does not exist:\n{self._external_tools_cfg.animepahe_repo_path}",
            )
            return

        if self._manager is not None:
            self._manager.set_external_tools_config(self._external_tools_cfg)
            if self._manager.is_animepahe_running():
                ok, msg = self._manager.stop_animepahe_scraper()
            else:
                ok, msg = self._manager.start_animepahe_scraper()
        else:
            ok, msg, proc = launch_animepahe_cli(self._external_tools_cfg)

        if ok:
            QMessageBox.information(self, "AnimePahe Scraper CLI", msg)
        else:
            QMessageBox.warning(self, "CLI Scraper", msg)

    def _on_animepahe_status_changed(self, is_running: bool):
        if hasattr(self, "_btn_run_cli_now"):
            if is_running:
                self._btn_run_cli_now.setText("⏹️ Stop CLI Scraper")
                self._btn_run_cli_now.setToolTip("Stop running AnimePahe background scraper")
            else:
                self._btn_run_cli_now.setText("▶️ Run CLI Now")
                self._btn_run_cli_now.setToolTip("Launch AnimePahe background scraper in CLI mode")

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

    def _on_add_backlog_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Backlog Folder")
        if folder:
            norm = normalize_path(folder)
            existing = [self._backlog_list.item(i).text() for i in range(self._backlog_list.count())]
            if norm not in existing:
                self._backlog_list.addItem(norm)

    def _on_add_backlog_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Backlog File", "", "Text Files (*.txt);;All Files (*)"
        )
        if file_path:
            norm = normalize_path(file_path)
            existing = [self._backlog_list.item(i).text() for i in range(self._backlog_list.count())]
            if norm not in existing:
                self._backlog_list.addItem(norm)

    def _on_remove_backlog_loc(self):
        for item in self._backlog_list.selectedItems():
            self._backlog_list.takeItem(self._backlog_list.row(item))

    def _on_reset_backlog_defaults(self):
        self._backlog_list.clear()
        defaults = [
            normalize_path(Path.cwd()),
            normalize_path(APP_DIR),
            normalize_path(Path.home()),
        ]
        for d in defaults:
            self._backlog_list.addItem(d)

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

    def _on_retry_exp_toggled(self, checked: bool):
        self._retry_factor_lbl.setEnabled(checked)
        self._retry_factor_spin.setEnabled(checked)
        self._retry_max_delay_lbl.setEnabled(checked)
        self._retry_max_delay_spin.setEnabled(checked)

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
            scan_after_download=True,
            scanner_type=scanner_type,
            custom_scanner_path=custom_path,
            custom_scanner_args=custom_args,
        )

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            tf.write(b"Safe test file for antivirus scanning verification.\n")
            temp_path = tf.name

        try:
            is_clean, report = scan_file(temp_path, cfg)
            scanner_display = f"Custom ({Path(custom_path).name})" if scanner_type == "custom" and custom_path else "Windows Defender"
            if is_clean:
                QMessageBox.information(
                    self, "Antivirus Scanner Test",
                    f"✅ Scanner verified successfully!\n\n"
                    f"Scanner: {scanner_display}\n"
                    f"Result: Clean (Safe)\n"
                    f"Details: {report}",
                )
            else:
                QMessageBox.warning(
                    self, "Antivirus Scanner Test",
                    f"⚠️ Scanner executed but detected a threat or returned non-zero code:\n\n"
                    f"Scanner: {scanner_display}\n"
                    f"Result: {report}",
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

    def _on_add_threat_exclusion(self):
        text = self._new_threat_excl_edit.text().strip()
        if not text:
            return
        items = [p.strip() for p in text.split(",") if p.strip()]
        existing = [
            self._threat_excl_list.item(i).text().strip().lower()
            for i in range(self._threat_excl_list.count())
        ]
        for item in items:
            if item.lower() not in existing:
                self._threat_excl_list.addItem(item)
                existing.append(item.lower())
        self._new_threat_excl_edit.clear()

    def _on_remove_threat_exclusion(self):
        row = self._threat_excl_list.currentRow()
        if row >= 0:
            self._threat_excl_list.takeItem(row)

    def _on_reset_threat_exclusions_defaults(self):
        self._threat_excl_list.clear()
        for cat in KNOWN_THREAT_CATEGORIES:
            self._threat_excl_list.addItem(cat)

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
        self._general_cfg.retry_exponential_backoff = self._retry_exp_cb.isChecked()
        self._general_cfg.retry_delay = self._retry_delay_spin.value()
        self._general_cfg.retry_backoff_factor = self._retry_factor_spin.value()
        self._general_cfg.retry_max_delay = float(self._retry_max_delay_spin.value())
        self._general_cfg.auto_resume_startup = self._auto_resume_cb.isChecked()
        self._general_cfg.notify_on_completion = self._notify_cb.isChecked()
        self._general_cfg.metadata_fetch_timeout_days = self._metadata_timeout_spin.value()
        locs = [self._backlog_list.item(i).text().strip() for i in range(self._backlog_list.count())]
        self._general_cfg.backlog_locations = [l for l in locs if l]
        self._general_cfg.clear_backlog_after_load = self._clear_backlog_cb.isChecked()
        self._general_cfg.backlog_poll_enabled = self._backlog_poll_cb.isChecked()
        self._general_cfg.backlog_poll_interval = self._backlog_poll_spin.value()
        self._general_cfg.save()

        # 2. Collect BitTorrent settings
        self._torrent_cfg.seeding_after_complete = self._seeding_after_complete_cb.isChecked()
        self._torrent_cfg.resume_seeding_on_startup = self._resume_seeding_cb.isChecked()
        self._torrent_cfg.seeding_time_limit_minutes = self._seeding_time_spin.value()
        self._torrent_cfg.seeding_ratio_limit = self._seeding_ratio_limit_spin.value()
        self._torrent_cfg.max_seeding_speed = self._max_seeding_speed_spin.value()
        self._torrent_cfg.download_to_seeding_ratio = self._seeding_ratio_spin.value()
        self._torrent_cfg.metadata_fetch_timeout_days = self._metadata_timeout_spin.value()
        self._torrent_cfg.save()

        # 3. Collect Network settings
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

        # 4. Collect Security settings
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
        self._security_cfg.scan_timing = (
            "manual_only" if self._timing_manual_rb.isChecked() else "after_complete"
        )
        excl_items = [
            self._threat_excl_list.item(i).text().strip()
            for i in range(self._threat_excl_list.count())
        ]
        self._security_cfg.ignored_threat_categories = [x for x in excl_items if x]
        self._security_cfg.ignored_threat_patterns = ""
        self._security_cfg.save()

        # 5. Collect Tor settings
        self._tor_cfg.enabled = self._tor_enable_cb.isChecked()
        self._tor_cfg.auto_start_at_startup = self._tor_autostart_cb.isChecked()
        self._tor_cfg.route_http = self._tor_route_http_cb.isChecked()
        self._tor_cfg.route_torrent = self._tor_route_torrent_cb.isChecked()
        self._tor_cfg.proxy_host = self._tor_host_edit.text().strip() or "127.0.0.1"
        self._tor_cfg.proxy_port = self._tor_port_spin.value()
        self._tor_cfg.tor_executable_path = self._tor_path_edit.text().strip()
        self._tor_cfg.save()

        # 6. Collect External Tools settings
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        self._external_tools_cfg.animepahe_launch_on_startup = self._animepahe_startup_cb.isChecked()
        self._external_tools_cfg.save()

        # 7. Collect Browser Integration settings
        self._browser_cfg.enabled = self._browser_enabled_cb.isChecked()
        self._browser_cfg.port = self._browser_port_spin.value()
        self._browser_cfg.intercept_all = self._browser_intercept_cb.isChecked()
        bypassed_text = self._browser_bypass_edit.text().strip()
        self._browser_cfg.bypassed_extensions = [
            ext.strip() for ext in bypassed_text.split(",") if ext.strip()
        ] if bypassed_text else []
        self._browser_cfg.save()
        if self._manager and hasattr(self._manager, "set_browser_config"):
            self._manager.set_browser_config(self._browser_cfg)

        self.accept()

    @property
    def general_config(self) -> GeneralConfig:
        return self._general_cfg

    @property
    def torrent_config(self) -> TorrentConfig:
        return self._torrent_cfg

    @property
    def network_config(self) -> NetworkConfig:
        return self._network_cfg

    @property
    def security_config(self) -> SecurityConfig:
        return self._security_cfg

    @property
    def tor_config(self) -> TorConfig:
        return self._tor_cfg

    @property
    def external_tools_config(self) -> ExternalToolsConfig:
        return self._external_tools_cfg

    @property
    def browser_config(self) -> BrowserIntegrationConfig:
        return self._browser_cfg
