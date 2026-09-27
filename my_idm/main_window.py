"""Main application window for My-IDM."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QPoint, QSettings, QPointF, QTimer, QByteArray, QRect, QRectF, QEvent, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from my_idm.database import Database, DownloadEntry
from my_idm.delegates import DownloadNameDelegate, ProgressBarDelegate, SavePathDelegate
from my_idm.details_panel import DetailsPanel
from my_idm.dialogs import (
    AddDownloadDialog,
    DeleteConfirmDialog,
    MoveDownloadDialog,
    RenameDialog,
)
from my_idm.download_model import Col, DownloadTableModel
from my_idm.header_view import FilterHeaderView
from my_idm.manager import DownloadManager
from my_idm.resources import get_app_icon, get_app_logo_pixmap
from my_idm.network import NetworkConfig, is_vpn_adapter_name
from my_idm.network_dialog import NetworkSettingsDialog
from my_idm.security import SecurityConfig
from my_idm.config import TorConfig
from my_idm.security_dialog import SecuritySettingsDialog
from my_idm.settings_dialog import SettingsDialog
from my_idm.external_tools import launch_animepahe_gui
from my_idm.styles import Colors

log = logging.getLogger(__name__)

SPEED_LIMIT_PRESETS = [
    ("Unlimited", 0),
    ("1 kbps", 1 * 1024),
    ("2 kbps", 2 * 1024),
    ("5 kbps", 5 * 1024),
    ("10 kbps", 10 * 1024),
    ("50 kbps", 50 * 1024),
    ("100 kbps", 100 * 1024),
    ("200 kbps", 200 * 1024),
    ("500 kbps", 500 * 1024),
    ("1 mbps", 1 * 1024 * 1024),
    ("2 mbps", 2 * 1024 * 1024),
    ("5 mbps", 5 * 1024 * 1024),
    ("10 mbps", 10 * 1024 * 1024),
    ("100 mbps", 100 * 1024 * 1024),
]


def _create_play_icon(size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#4ade80"))
    p.setPen(Qt.PenStyle.NoPen)
    triangle = QPolygonF([
        QPointF(size * 0.25, size * 0.18),
        QPointF(size * 0.82, size * 0.5),
        QPointF(size * 0.25, size * 0.82),
    ])
    p.drawPolygon(triangle)
    p.end()
    return QIcon(pix)


def _create_pause_icon(size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#38bdf8"))
    p.setPen(Qt.PenStyle.NoPen)
    bar_w = size * 0.22
    bar_h = size * 0.64
    y = size * 0.18
    p.drawRoundedRect(size * 0.2, y, bar_w, bar_h, 2, 2)
    p.drawRoundedRect(size * 0.58, y, bar_w, bar_h, 2, 2)
    p.end()
    return QIcon(pix)


def _create_stop_icon(size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#ef4444"))
    p.setPen(Qt.PenStyle.NoPen)
    margin = size * 0.2
    side = size - 2 * margin
    p.drawRoundedRect(margin, margin, side, side, 3, 3)
    p.end()
    return QIcon(pix)


def _create_emoji_icon(emoji: str, size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = QFont(["Segoe UI Emoji", "Noto Color Emoji", "Apple Color Emoji", "sans-serif"])
    font.setPixelSize(int(size * 0.65))
    p.setFont(font)
    p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, emoji)
    p.end()
    return QIcon(pix)


def _create_details_panel_icon(size: int = 32) -> QIcon:
    """Create a sleek icon showing window layout with bottom details panel highlighted."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Window outer outline
    pen = QPen(QColor("#8b949e"), max(1.5, size * 0.065))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(size * 0.12, size * 0.12, size * 0.76, size * 0.76)
    p.drawRoundedRect(rect, 3, 3)

    # Divider line
    p.setPen(QPen(QColor("#8b949e"), max(1.2, size * 0.055)))
    p.drawLine(QPointF(size * 0.12, size * 0.54), QPointF(size * 0.88, size * 0.54))

    # Highlighted bottom details panel
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#58a6ff"))
    bot_rect = QRectF(size * 0.16, size * 0.58, size * 0.68, size * 0.26)
    p.drawRoundedRect(bot_rect, 2, 2)

    p.end()
    return QIcon(pix)


class MainWindow(QMainWindow):
    """The main My-IDM window."""

    _sig_show_tray_notification = Signal(str, str, int)

    def __init__(
        self,
        manager: DownloadManager,
        show_exit_splash: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._manager = manager
        self._show_exit_splash = show_exit_splash
        self._tray_icon: Optional[QSystemTrayIcon] = None
        self._force_exit: bool = False
        self._close_to_tray_notified: bool = False
        self._completed_notified: set[str] = set()

        self.setWindowTitle("My-IDM — Download Manager")
        self.setMinimumSize(1100, 600)
        self.resize(1400, 750)
        self.setWindowIcon(get_app_icon())

        # Model
        self._model = DownloadTableModel(self)

        self._setup_ui()
        self._setup_actions()
        self._setup_toolbar()
        self._setup_menubar()
        self._setup_statusbar()
        self._setup_system_tray()
        self._connect_signals()

        # Restore window geometry, location, column lengths, and splitter from DB
        self._restore_ui_state_from_db()

        # Load existing downloads from DB
        self._load_history()

    # -- UI setup ------------------------------------------------------------

    def _setup_ui(self):
        # Table view
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._table.setSortingEnabled(True)
        self._last_sort_section = Col.ADDED
        self._table.sortByColumn(
            Col.ADDED, Qt.SortOrder.DescendingOrder
        )
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)
        self._table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )

        self._name_delegate = DownloadNameDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.NAME, self._name_delegate
        )

        # Progress bar delegate
        self._progress_delegate = ProgressBarDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.PROGRESS, self._progress_delegate
        )

        # Save path delegate with intelligent shortening
        self._save_path_delegate = SavePathDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.SAVE_PATH, self._save_path_delegate
        )

        self._file_name_delegate = DownloadNameDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.FILE_NAME, self._file_name_delegate
        )

        # Filterable and movable column header with sort indicators
        self._header_view = FilterHeaderView(self._table)
        self._table.setHorizontalHeader(self._header_view)
        header = self._header_view
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setCascadingSectionResizes(False)
        header.setDefaultSectionSize(110)
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)
        header.sectionMoved.connect(self._on_section_moved)
        header.filter_requested.connect(self._on_header_filter_requested)
        header.sectionClicked.connect(self._on_header_section_clicked)

        self._model.set_tor_config(self._manager.tor_config)
        self._table.clicked.connect(self._on_table_clicked)
        self._table.doubleClicked.connect(self._on_table_double_clicked)
        self._model.modelReset.connect(self._apply_table_spans)
        self._model.layoutChanged.connect(self._apply_table_spans)

        # Segregated view: disabled by default, state and mode persisted in db
        self._segregated_view_enabled = bool(self._manager.db.get_ui_state("segregated_view_enabled", False))
        self._segregated_view_mode = str(self._manager.db.get_ui_state("segregated_view_mode", "status"))
        if self._segregated_view_mode not in ("status", "date"):
            self._segregated_view_mode = "status"

        all_sec_ids = (
            "active", "seeding", "inactive",
            "date_today", "date_yesterday",
            "date_last_7_days", "date_this_week",
            "date_last_30_days", "date_this_month",
            "date_older",
        )
        for sec_id in all_sec_ids:
            if self._manager.db.get_ui_state(f"segregated_{sec_id}_collapsed", False):
                canonical_sec_id = sec_id
                if sec_id == "date_this_week":
                    canonical_sec_id = "date_last_7_days"
                elif sec_id == "date_this_month":
                    canonical_sec_id = "date_last_30_days"
                self._model.set_section_collapsed(canonical_sec_id, True)
        self._model.set_segregated_view(self._segregated_view_enabled, mode=self._segregated_view_mode)
        self._apply_table_spans()

        # Move Col.SOURCE_DOMAIN and Col.FILE_NAME to the end of the table by default
        header.moveSection(header.visualIndex(Col.SOURCE_DOMAIN), Col.COUNT - 2)
        header.moveSection(header.visualIndex(Col.FILE_NAME), Col.COUNT - 1)

        # Set specific default column widths
        self._table.setColumnWidth(Col.QUEUE, 45)
        self._table.setColumnWidth(Col.NAME, 270)
        self._table.setColumnWidth(Col.SOURCE_DOMAIN, 160)
        self._table.setColumnWidth(Col.SIZE, 90)
        self._table.setColumnWidth(Col.PROGRESS, 160)
        self._table.setColumnWidth(Col.STATUS, 135)
        self._table.setColumnWidth(Col.SPEED, 110)
        self._table.setColumnWidth(Col.ETA, 80)
        self._table.setColumnWidth(Col.SEEDS_PEERS, 100)
        self._table.setColumnWidth(Col.ADDED, 130)
        self._table.setColumnWidth(Col.LAST_TRIED, 130)
        self._table.setColumnWidth(Col.COMPLETED, 130)
        self._table.setColumnWidth(Col.SAVE_PATH, 220)
        self._table.setColumnWidth(Col.FILE_NAME, 220)

        # Row height
        self._table.verticalHeader().setDefaultSectionSize(36)

        # Context menu
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._table.customContextMenuRequested.connect(self._show_context_menu)

        # Splitter with download table on top and details panel on bottom
        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._splitter.addWidget(self._table)
        self._details_panel = DetailsPanel(self._manager, self)
        self._details_panel.setMinimumHeight(140)
        if hasattr(self._details_panel, "browser_container_hwnd"):
            self._manager.set_browser_container_hwnd(self._details_panel.browser_container_hwnd)
        self._details_panel.browser_tab_requested.connect(self._on_browser_tab_requested)
        self._splitter.addWidget(self._details_panel)
        self._splitter.setChildrenCollapsible(False)
        self._details_height: int = 250
        self._splitter.setSizes([450, self._details_height])
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

        self.setCentralWidget(self._splitter)

    def _on_browser_tab_requested(self):
        """Ensure bottom panel is visible and expanded when an external browser session opens."""
        # Avoid expanding details panel while in tray (window hidden)
        if not self.isVisible() or self.isMinimized():
            return
        if hasattr(self, "_act_details") and not self._act_details.isChecked():
            self._act_details.setChecked(True)
            self._on_details_toggle(True)
        sizes = self._splitter.sizes()
        if len(sizes) == 2 and sizes[1] < 340:
            total = sum(sizes)
            top_h = max(100, total - 360)
            bot_h = total - top_h
            self._splitter.setSizes([top_h, bot_h])

    def _on_splitter_moved(self, pos: int, index: int):
        """Track user-adjusted details panel height in real time."""
        if self._details_panel.isVisible():
            sizes = self._splitter.sizes()
            if len(sizes) == 2 and sizes[1] >= 50:
                self._details_height = sizes[1]

    def _setup_actions(self):
        """Create all QActions."""
        self._act_add = QAction(_create_emoji_icon("➕"), "Add Download", self)
        self._act_add.setShortcut(QKeySequence("Ctrl+N"))
        self._act_add.setToolTip("Add URL, Magnet Link, or .torrent file (Ctrl+N)")
        self._act_add.triggered.connect(self._on_add)

        self._act_add_torrent = QAction(_create_emoji_icon("📦"), "Add Torrent File…", self)
        self._act_add_torrent.setShortcut(QKeySequence("Ctrl+T"))
        self._act_add_torrent.setToolTip("Add .torrent file (Ctrl+T)")
        self._act_add_torrent.triggered.connect(self._on_add_torrent)

        self._act_resume = QAction(_create_play_icon(), "Resume", self)
        self._act_resume.setShortcut(QKeySequence("Ctrl+R"))
        self._act_resume.setToolTip("Resume selected downloads (Ctrl+R)")
        self._act_resume.triggered.connect(self._on_resume)

        self._act_force_start = QAction(_create_emoji_icon("⚡"), "Force Start", self)
        self._act_force_start.setToolTip("Force start selected download(s) immediately")
        self._act_force_start.triggered.connect(self._on_force_start)

        self._act_pause = QAction(_create_pause_icon(), "Pause", self)
        self._act_pause.setShortcut(QKeySequence("Space"))
        self._act_pause.setToolTip("Pause selected downloads (Space)")
        self._act_pause.triggered.connect(self._on_pause)

        self._act_stop = QAction(_create_stop_icon(), "Stop", self)
        self._act_stop.setToolTip("Stop selected downloads permanently until manually resumed")
        self._act_stop.triggered.connect(self._on_stop)

        self._act_start_seeding = QAction(_create_emoji_icon("🌱"), "Start Seeding", self)
        self._act_start_seeding.setToolTip("Start or resume seeding for completed torrents")
        self._act_start_seeding.triggered.connect(self._on_start_seeding)

        self._act_pause_all = QAction(_create_emoji_icon("⏸️"), "Pause All", self)
        self._act_pause_all.setToolTip("Pause all active and queued downloads")
        self._act_pause_all.triggered.connect(self._on_pause_all_downloads)

        self._act_stop_all_seeding = QAction(_create_emoji_icon("🛑"), "Stop All Seeding", self)
        self._act_stop_all_seeding.setToolTip("Stop all active seeding torrents (move to Completed)")
        self._act_stop_all_seeding.triggered.connect(self._on_stop_all_seeding)

        self._act_copy_url = QAction(_create_emoji_icon("📋"), "Copy URL / Magnet", self)
        self._act_copy_url.setShortcut(QKeySequence("Ctrl+C"))
        self._act_copy_url.setToolTip("Copy download URL or Magnet link to clipboard (Ctrl+C)")
        self._act_copy_url.triggered.connect(self._on_copy_url)

        self._act_rename = QAction(_create_emoji_icon("✏️"), "Rename…", self)
        self._act_rename.setShortcut(QKeySequence("F2"))
        self._act_rename.setToolTip("Rename downloaded file or folder (F2)")
        self._act_rename.triggered.connect(self._on_rename)

        self._act_delete = QAction(_create_emoji_icon("🗑"), "Delete", self)
        self._act_delete.setShortcut(QKeySequence("Delete"))
        self._act_delete.setToolTip("Delete selected downloads")
        self._act_delete.triggered.connect(self._on_delete)

        self._act_delete_file = QAction(_create_emoji_icon("🗑"), "Delete File", self)
        self._act_delete_file.setToolTip("Delete downloaded file from disk (move to Trash), keeping entry paused at 0%")
        self._act_delete_file.triggered.connect(self._on_delete_file)

        self._act_move = QAction(_create_emoji_icon("📂"), "Move…", self)
        self._act_move.setToolTip("Move download to another directory")
        self._act_move.triggered.connect(self._on_move)

        self._act_recheck = QAction(_create_emoji_icon("🔄"), "Recheck", self)
        self._act_recheck.setToolTip("Verify existing files on disk")
        self._act_recheck.triggered.connect(self._on_recheck)

        self._act_open_file = QAction(_create_emoji_icon("📄"), "Open File", self)
        self._act_open_file.setShortcut(QKeySequence("Return"))
        self._act_open_file.setToolTip("Open the downloaded file")
        self._act_open_file.triggered.connect(self._on_open_file)

        self._act_open_folder = QAction(_create_emoji_icon("📁"), "Open Folder", self)
        self._act_open_folder.setShortcut(QKeySequence("Ctrl+O"))
        self._act_open_folder.setToolTip("Open containing folder (Ctrl+O)")
        self._act_open_folder.triggered.connect(self._on_open_folder)

        self._act_load_backlog = QAction(_create_emoji_icon("📋"), "Load Backlog…", self)
        self._act_load_backlog.setShortcut(QKeySequence("Ctrl+L"))
        self._act_load_backlog.setToolTip("Load URLs from a backlog file")
        self._act_load_backlog.triggered.connect(self._on_load_backlog)

        self._act_move_up = QAction(_create_emoji_icon("⬆"), "Move Up in Queue", self)
        self._act_move_up.setToolTip("Move selected download up in queue order")
        self._act_move_up.triggered.connect(self._on_move_queue_up)

        self._act_move_down = QAction(_create_emoji_icon("⬇"), "Move Down in Queue", self)
        self._act_move_down.setToolTip("Move selected download down in queue order")
        self._act_move_down.triggered.connect(self._on_move_queue_down)

        self._act_preferences = QAction(_create_emoji_icon("⚙"), "Preferences…", self)
        self._act_preferences.setShortcut(QKeySequence("Ctrl+,"))
        self._act_preferences.setToolTip(
            "Configure default download folder, performance, network, and security (Ctrl+,)"
        )
        self._act_preferences.triggered.connect(
            lambda: self._on_open_preferences(0)
        )

        self._act_torrent_settings = QAction(_create_emoji_icon("🧲"), "BitTorrent Settings…", self)
        self._act_torrent_settings.setToolTip(
            "Configure BitTorrent seeding behavior, speed limits, and metadata timeout"
        )
        self._act_torrent_settings.triggered.connect(
            self._on_open_torrent_settings
        )

        self._act_network_settings = QAction(_create_emoji_icon("🌐"), "VPN & Network Settings…", self)
        self._act_network_settings.setToolTip(
            "Configure VPN adapter binding, Kill Switch, and Proxy"
        )
        self._act_network_settings.triggered.connect(
            self._on_open_network_settings
        )

        self._act_security_settings = QAction(_create_emoji_icon("🛡"), "Antivirus & Security Settings…", self)
        self._act_security_settings.setToolTip(
            "Configure pre-download URL inspection and post-download antivirus scanning"
        )
        self._act_security_settings.triggered.connect(
            self._on_open_security_settings
        )

        self._act_scan_antivirus = QAction(_create_emoji_icon("🛡"), "Scan with Antivirus", self)
        self._act_scan_antivirus.setToolTip("Scan the downloaded file with antivirus")
        self._act_scan_antivirus.triggered.connect(self._on_scan_selected_file)

        self._act_toggle_details = QAction(_create_details_panel_icon(), "Details Panel", self)
        self._act_toggle_details.setCheckable(True)
        self._act_toggle_details.setChecked(True)
        self._act_toggle_details.setShortcut(QKeySequence("F4"))
        self._act_toggle_details.setToolTip("Hide bottom download details panel (F4)")
        self._act_toggle_details.toggled.connect(self._on_toggle_details)
        self._details_panel.close_requested.connect(
            lambda: self._act_toggle_details.setChecked(False)
        )

        self._act_reset_view = QAction(_create_emoji_icon("🔄"), "Reset View", self)
        self._act_reset_view.setToolTip("Reset column visibility, filters, sorting, and column widths to defaults")
        self._act_reset_view.triggered.connect(self._on_reset_view)

        self._act_tor = QAction(_create_emoji_icon("🧅"), "Tor: OFF", self)
        self._act_tor.setCheckable(False)
        self._act_tor.setToolTip("Toggle Tor network privacy routing (SOCKS5 proxy)")
        self._act_tor.triggered.connect(
            lambda: self._on_toggle_tor(not self._manager.tor_config.enabled)
        )

    def _setup_toolbar(self):
        toolbar = QToolBar("Main Toolbar")
        self._toolbar = toolbar
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        toolbar.addAction(self._act_add)
        toolbar.addSeparator()
        toolbar.addAction(self._act_resume)
        toolbar.addAction(self._act_pause)
        toolbar.addAction(self._act_stop)
        toolbar.addAction(self._act_start_seeding)
        toolbar.addSeparator()
        toolbar.addAction(self._act_pause_all)
        toolbar.addAction(self._act_stop_all_seeding)
        toolbar.addSeparator()
        toolbar.addAction(self._act_delete)
        toolbar.addAction(self._act_move)
        toolbar.addAction(self._act_recheck)
        toolbar.addSeparator()

        # Tor toolbar control with embedded progress bar
        self._tor_toolbar_container = QWidget()
        self._tor_toolbar_container.setObjectName("tor_toolbar_container")
        self._tor_toolbar_container.setStyleSheet("background: transparent;")
        self._tor_toolbar_container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        tor_tb_layout = QVBoxLayout(self._tor_toolbar_container)
        tor_tb_layout.setContentsMargins(0, 0, 0, 0)
        tor_tb_layout.setSpacing(1)

        self._tor_toolbar_btn = QToolButton()
        self._tor_toolbar_btn.setDefaultAction(self._act_tor)
        self._tor_toolbar_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tor_tb_layout.addWidget(self._tor_toolbar_btn)

        self._tor_toolbar_progress = QProgressBar()
        self._tor_toolbar_progress.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._tor_toolbar_progress.setFixedHeight(4)
        self._tor_toolbar_progress.setTextVisible(False)
        self._tor_toolbar_progress.setVisible(False)
        self._tor_toolbar_progress.setStyleSheet("""
            QProgressBar {
                background: #21252b;
                border: none;
                border-radius: 2px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #50fa7b, stop:1 #8be9fd);
                border-radius: 2px;
            }
        """)
        tor_tb_layout.addWidget(self._tor_toolbar_progress)

        toolbar.addWidget(self._tor_toolbar_container)
        toolbar.addSeparator()
        toolbar.addAction(self._act_preferences)

        # Show only icons without text for playback and action buttons
        for act in (
            self._act_resume,
            self._act_pause,
            self._act_stop,
            self._act_start_seeding,
            self._act_pause_all,
            self._act_stop_all_seeding,
            self._act_delete,
            self._act_move,
            self._act_recheck,
        ):
            btn = toolbar.widgetForAction(act)
            if isinstance(btn, QToolButton):
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        # Expanding spacer pushes subsequent controls to the right
        spacer = QWidget()
        spacer.setObjectName("toolbar_spacer")
        spacer.setStyleSheet("background: transparent;")
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.addToolBar(toolbar)

    def _setup_menubar(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(self._act_add)
        file_menu.addAction(self._act_add_torrent)
        file_menu.addAction(self._act_load_backlog)
        file_menu.addSeparator()

        exit_act = QAction(_create_emoji_icon("🚪"), "Exit", self)
        exit_act.setShortcut(QKeySequence("Ctrl+Q"))
        exit_act.triggered.connect(self._exit_app)
        file_menu.addAction(exit_act)

        # Edit menu
        edit_menu = menubar.addMenu("&Edit")
        edit_menu.addAction(self._act_resume)
        edit_menu.addAction(self._act_force_start)
        edit_menu.addAction(self._act_pause)
        edit_menu.addAction(self._act_pause_all)
        edit_menu.addAction(self._act_stop)
        edit_menu.addAction(self._act_start_seeding)
        edit_menu.addAction(self._act_stop_all_seeding)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_move_up)
        edit_menu.addAction(self._act_move_down)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_copy_url)
        edit_menu.addAction(self._act_rename)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_delete)
        edit_menu.addAction(self._act_move)
        edit_menu.addAction(self._act_recheck)

        # View menu
        view_menu = menubar.addMenu("&View")
        self._menu_segregated_view = view_menu.addMenu(_create_emoji_icon("🗂️"), "Segregated View")

        self._act_segregated_view = QAction("On", self)
        self._act_segregated_view.setCheckable(True)
        self._act_segregated_view.setChecked(self._segregated_view_enabled)
        self._act_segregated_view.toggled.connect(self._on_toggle_segregated_view)
        self._menu_segregated_view.addAction(self._act_segregated_view)

        self._menu_segregated_view.addSeparator()

        self._seg_mode_group = QActionGroup(self)
        self._seg_mode_group.setExclusive(True)

        self._act_seg_by_status = QAction("Status (Active / Seeding / Inactive)", self)
        self._act_seg_by_status.setCheckable(True)
        self._act_seg_by_status.setChecked(self._segregated_view_mode == "status")
        self._act_seg_by_status.triggered.connect(lambda: self._set_segregation_mode("status"))
        self._seg_mode_group.addAction(self._act_seg_by_status)
        self._menu_segregated_view.addAction(self._act_seg_by_status)

        self._act_seg_by_date = QAction("Date (Today / Yesterday / Last 7 Days / Last 30 Days / Older)", self)
        self._act_seg_by_date.setCheckable(True)
        self._act_seg_by_date.setChecked(self._segregated_view_mode == "date")
        self._act_seg_by_date.triggered.connect(lambda: self._set_segregation_mode("date"))
        self._seg_mode_group.addAction(self._act_seg_by_date)
        self._menu_segregated_view.addAction(self._act_seg_by_date)

        view_menu.addAction(self._act_toggle_details)
        view_menu.addSeparator()
        select_all_act = QAction(_create_emoji_icon("☑️"), "Select All", self)
        select_all_act.setShortcut(QKeySequence("Ctrl+A"))
        select_all_act.triggered.connect(self._table.selectAll)
        view_menu.addAction(select_all_act)

        view_menu.addSeparator()
        view_menu.addAction(self._act_reset_view)

        view_menu.addSeparator()
        sort_menu = view_menu.addMenu("&Sort By")
        sort_menu.setIcon(_create_emoji_icon("↕️"))
        sort_columns = [
            ("Date Added (Default)", Col.ADDED),
            ("Name", Col.NAME),
            ("Source Domain", Col.SOURCE_DOMAIN),
            ("Size", Col.SIZE),
            ("Progress", Col.PROGRESS),
            ("Status", Col.STATUS),
            ("Speed", Col.SPEED),
            ("ETA", Col.ETA),
            ("Date Last Tried", Col.LAST_TRIED),
            ("Date Completed", Col.COMPLETED),
        ]
        for title, col_idx in sort_columns:
            act = QAction(title, self)
            act.triggered.connect(
                lambda checked=False, c=col_idx: self._sort_by_column(c)
            )
            sort_menu.addAction(act)

        sort_menu.addSeparator()
        act_asc = QAction("Ascending", self)
        act_asc.triggered.connect(
            lambda: self._set_sort_order(Qt.SortOrder.AscendingOrder)
        )
        sort_menu.addAction(act_asc)

        act_desc = QAction("Descending", self)
        act_desc.triggered.connect(
            lambda: self._set_sort_order(Qt.SortOrder.DescendingOrder)
        )
        sort_menu.addAction(act_desc)

        # Tools menu
        tools_menu = menubar.addMenu("&Tools")
        tools_menu.addAction(self._act_preferences)
        self._act_export_csv = QAction(_create_emoji_icon("📄"), "Export Selected as CSV…", self)
        self._act_export_csv.triggered.connect(self._on_export_selected_csv)
        tools_menu.addAction(self._act_export_csv)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_torrent_settings)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_tor)
        self._act_tor_settings = QAction(_create_emoji_icon("🧅"), "Tor Network Settings…", self)
        self._act_tor_settings.triggered.connect(self._on_open_tor_settings)
        tools_menu.addAction(self._act_tor_settings)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_network_settings)
        tools_menu.addAction(self._act_security_settings)
        tools_menu.addSeparator()
        self._act_launch_animepahe_gui = QAction(_create_emoji_icon("🎬"), "Launch AnimePahe Downloader…", self)
        self._act_launch_animepahe_gui.triggered.connect(self._on_launch_animepahe_gui)
        tools_menu.addAction(self._act_launch_animepahe_gui)
        self._act_run_animepahe_cli = QAction(_create_emoji_icon("▶️"), "Run AnimePahe Scraper (CLI)", self)
        self._act_run_animepahe_cli.triggered.connect(self._on_start_animepahe_cli)
        tools_menu.addAction(self._act_run_animepahe_cli)
        self._act_external_tools_settings = QAction(_create_emoji_icon("🛠️"), "External Tools Settings…", self)
        self._act_external_tools_settings.triggered.connect(self._on_open_external_tools_settings)
        tools_menu.addAction(self._act_external_tools_settings)
        tools_menu.addSeparator()
        self._act_browser_settings = QAction(_create_emoji_icon("🌐"), "Browser Integration Settings…", self)
        self._act_browser_settings.triggered.connect(self._on_open_browser_settings)
        tools_menu.addAction(self._act_browser_settings)

        # Help menu
        help_menu = menubar.addMenu("&Help")
        about_act = QAction(_create_emoji_icon("ℹ️"), "About My-IDM", self)
        about_act.triggered.connect(self._on_about)
        help_menu.addAction(about_act)

    def _setup_statusbar(self):
        self._status_label = QLabel("Ready")
        self._speed_label = QLabel("")
        self._speed_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._speed_label.customContextMenuRequested.connect(self._show_speed_context_menu)
        self._speed_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._speed_label.setToolTip("Total Transfer Speed (Click or right-click to set Download / Upload limits)")

        def _speed_label_mouse_press(event):
            if event.button() == Qt.MouseButton.LeftButton:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                self._show_speed_context_menu(pos)
            QLabel.mousePressEvent(self._speed_label, event)

        self._speed_label.mousePressEvent = _speed_label_mouse_press
        self._count_label = QLabel("0 Downloads, 0 Active")

        # Tor footer widget with button and embedded progress bar
        self._tor_footer_container = QWidget()
        tor_footer_layout = QVBoxLayout(self._tor_footer_container)
        tor_footer_layout.setContentsMargins(0, 0, 0, 0)
        tor_footer_layout.setSpacing(1)

        self._tor_status_btn = QPushButton("🧅 Tor: OFF")
        self._tor_status_btn.setFlat(True)
        self._tor_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tor_status_btn.setToolTip("Click to configure Tor Network Settings")
        self._tor_status_btn.clicked.connect(self._on_open_tor_settings)
        tor_footer_layout.addWidget(self._tor_status_btn)

        self._tor_footer_progress = QProgressBar()
        self._tor_footer_progress.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._tor_footer_progress.setFixedHeight(3)
        self._tor_footer_progress.setTextVisible(False)
        self._tor_footer_progress.setVisible(False)
        self._tor_footer_progress.setStyleSheet("""
            QProgressBar {
                background: #21252b;
                border: none;
                border-radius: 1px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #50fa7b, stop:1 #8be9fd);
                border-radius: 1px;
            }
        """)
        tor_footer_layout.addWidget(self._tor_footer_progress)

        self._vpn_status_btn = QPushButton("🌐 Net: Default")
        self._vpn_status_btn.setFlat(True)
        self._vpn_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vpn_status_btn.setToolTip(
            "Click to configure VPN & Network Settings"
        )
        self._vpn_status_btn.clicked.connect(self._on_open_network_settings)

        self._details_status_btn = QPushButton("📋 Details: ON")
        self._details_status_btn.setFlat(True)
        self._details_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._details_status_btn.setToolTip("Toggle bottom download details panel (F4)")
        self._details_status_btn.clicked.connect(self._on_toggle_details_btn_clicked)

        self._console_status_btn = QPushButton("📄 Console: OFF")
        self._console_status_btn.setFlat(True)
        self._console_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._console_status_btn.setToolTip("Toggle bottom AnimePahe CLI Console panel")
        self._console_status_btn.clicked.connect(self._on_toggle_console_btn_clicked)
        self._animepahe_console_btn = self._console_status_btn

        # AnimePahe background scraper status badge
        self._animepahe_status_btn = QPushButton("🎬 AnimePahe: Active")
        self._animepahe_status_btn.setFlat(True)
        self._animepahe_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._animepahe_status_btn.setToolTip(
            "AnimePahe CLI Scraper is running in the background\nClick for options (View Logs, Stop, Settings)"
        )
        self._animepahe_status_btn.clicked.connect(self._show_animepahe_status_menu)
        self._animepahe_status_btn.setVisible(False)
        self._animepahe_status_btn.setStyleSheet("""
            QPushButton {
                background: #193524;
                color: #50fa7b;
                border: 1px solid #50fa7b;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #2a4030;
                border-color: #69ff94;
            }
        """)

        status_bar = QStatusBar()
        status_bar.addWidget(self._status_label, 1)
        status_bar.addPermanentWidget(self._animepahe_status_btn)
        status_bar.addPermanentWidget(self._tor_footer_container)
        status_bar.addPermanentWidget(self._vpn_status_btn)
        status_bar.addPermanentWidget(self._details_status_btn)
        status_bar.addPermanentWidget(self._console_status_btn)
        status_bar.addPermanentWidget(self._speed_label)
        status_bar.addPermanentWidget(self._count_label)
        self.setStatusBar(status_bar)

        self._on_animepahe_status_changed(self._manager.is_animepahe_running())
        self._sync_panel_buttons()

        self._update_network_status_badge(self._manager.network_config)
        self._on_tor_config_changed(self._manager.tor_config)
        self._update_speed_label()
        self._update_count_label()

    def _setup_system_tray(self):
        """Initializes the Windows system tray icon and context menu."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon = None
            return

        icon = get_app_icon()
        self._tray_icon = QSystemTrayIcon(icon, self)
        self._tray_icon.setToolTip("My-IDM — Download Manager")

        # Tray Context Menu
        tray_menu = QMenu(self)
        self._tray_act_toggle = QAction("🪟 Show My-IDM", self)
        self._tray_act_toggle.triggered.connect(self._toggle_show_window)
        tray_menu.addAction(self._tray_act_toggle)
        tray_menu.addSeparator()

        act_pause_all = QAction("⏸️ Pause All Downloads", self)
        act_pause_all.triggered.connect(self._on_pause_all_downloads)
        tray_menu.addAction(act_pause_all)

        act_resume_all = QAction("▶️ Resume All Downloads", self)
        act_resume_all.triggered.connect(self._on_resume_all_downloads)
        tray_menu.addAction(act_resume_all)
        tray_menu.addSeparator()

        act_prefs = QAction("⚙️ Preferences…", self)
        act_prefs.triggered.connect(lambda: self._on_open_preferences(0))
        tray_menu.addAction(act_prefs)
        tray_menu.addSeparator()

        act_exit = QAction("🚪 Exit My-IDM", self)
        act_exit.triggered.connect(self._exit_app)
        tray_menu.addAction(act_exit)

        self._tray_icon.setContextMenu(tray_menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.messageClicked.connect(self._on_tray_message_clicked)

        # Thread-safe notification dispatcher
        self._sig_show_tray_notification.connect(self._do_show_tray_notification)
        from my_idm.notifications import register_notification_handler
        register_notification_handler(self.show_tray_notification)

        if self._manager.general_config.enable_system_tray:
            self._tray_icon.show()
        self._update_tray_menu_text()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """Handles user clicking or double-clicking the system tray icon."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_show_window()

    def _on_tray_message_clicked(self):
        """Handles notification click to open and focus My-IDM window."""
        self._restore_and_focus()

    def _restore_and_focus(self):
        """Restores the window from tray/minimized state and brings it into active foreground focus."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        from my_idm.single_instance import activate_window
        activate_window(self)
        self._update_tray_menu_text()

    def _toggle_show_window(self):
        """Toggles main window visibility between foreground and hidden."""
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._restore_and_focus()
        self._update_tray_menu_text()

    def _update_tray_menu_text(self):
        """Updates the text of the tray context menu Show/Hide action dynamically."""
        if hasattr(self, "_tray_act_toggle") and self._tray_act_toggle:
            if self.isVisible() and not self.isMinimized():
                self._tray_act_toggle.setText("🪟 Hide My-IDM")
            else:
                self._tray_act_toggle.setText("🪟 Show My-IDM")

    def show_tray_notification(self, title: str, message: str, duration: int = 5, icon_path: Optional[str] = None) -> bool:
        """Handler registered with my_idm.notifications to route toasts via QSystemTrayIcon."""
        if not self._tray_icon or not self._tray_icon.isVisible():
            return False
        self._sig_show_tray_notification.emit(title, message, duration * 1000)
        return True

    def _do_show_tray_notification(self, title: str, message: str, msecs: int):
        """Displays toast on main GUI thread via QSystemTrayIcon."""
        if self._tray_icon and self._tray_icon.isVisible():
            try:
                self._tray_icon.showMessage(
                    title,
                    message,
                    QSystemTrayIcon.MessageIcon.Information,
                    msecs,
                )
            except Exception as exc:
                log.warning("Failed to show tray notification: %s", exc)

    def _exit_app(self):
        """Forces full application exit, bypassing close-to-tray intercept."""
        self._force_exit = True
        if self._tray_icon:
            self._tray_icon.hide()
        from my_idm.notifications import unregister_notification_handler
        unregister_notification_handler(self.show_tray_notification)

        from PySide6.QtGui import QCloseEvent
        close_ev = QCloseEvent()
        self.closeEvent(close_ev)

        app = QApplication.instance()
        if app:
            app.quit()

    def _connect_signals(self):
        self._manager.progress_updated.connect(self._on_progress_updated)
        self._manager.status_changed.connect(self._on_status_changed)
        self._manager.filename_resolved.connect(self._on_filename_resolved)
        self._manager.download_added.connect(self._on_download_added)
        self._manager.download_removed.connect(self._on_download_removed)
        self._manager.download_moved.connect(self._on_download_moved)
        self._manager.download_renamed.connect(self._on_download_renamed)
        self._manager.network_config_changed.connect(
            self._update_network_status_badge
        )
        self._manager.tor_config_changed.connect(self._on_tor_config_changed)
        self._manager.tor_status_changed.connect(self._on_tor_status_changed)
        self._manager.threat_detected.connect(self._on_threat_detected)
        self._manager.queue_order_changed.connect(self._on_queue_order_changed)
        self._manager.bandwidth_limits_changed.connect(self._on_bandwidth_limits_changed)
        self._manager.animepahe_status_changed.connect(
            self._on_animepahe_status_changed
        )
        if hasattr(self._manager, "animepahe_queue_changed"):
            self._manager.animepahe_queue_changed.connect(
                self._on_animepahe_queue_changed
            )
        self._details_panel.mode_changed.connect(lambda _: self._sync_panel_buttons())

        # Connect table selection to bottom details panel
        self._table.selectionModel().selectionChanged.connect(
            self._on_table_selection_changed
        )

        # Periodic timer for refreshing live details panel stats (speed, peers, progress)
        self._details_timer = QTimer(self)
        self._details_timer.setInterval(1000)
        self._details_timer.timeout.connect(self._on_details_timer_tick)
        self._details_timer.start()

    # -- data loading --------------------------------------------------------

    def _load_history(self):
        entries = self._manager.get_all_entries()
        self._model.load_entries(entries)
        self._update_count_label()
        self._update_speed_label()

    # -- selected entries helper ---------------------------------------------

    def _selected_ids(self) -> list[str]:
        return self._model.get_selected_ids(
            self._table.selectionModel().selectedIndexes()
        )

    def _first_selected_entry(self) -> Optional[DownloadEntry]:
        ids = self._selected_ids()
        if ids:
            return self._model.get_entry_by_id(ids[0])
        return None

    # -- sorting & column helpers --------------------------------------------

    def _on_header_filter_requested(self, column: int, selected_keys: object):
        self._update_count_label()

    def _on_section_moved(self, logical_index: int, old_visual: int, new_visual: int):
        if not hasattr(self, "_splitter"):
            return
        if old_visual != new_visual:
            self._save_ui_state_to_db()

    def _on_header_section_clicked(self, logical_index: int):
        if logical_index in Col.DATE_COLUMNS:
            # If switching to a date column from another column, ensure it defaults to Descending (newest first)
            if self._last_sort_section != logical_index:
                self._table.sortByColumn(logical_index, Qt.SortOrder.DescendingOrder)
        self._last_sort_section = logical_index

    def _sort_by_column(self, col: int):
        if col in Col.DATE_COLUMNS:
            curr_sec = self._table.horizontalHeader().sortIndicatorSection()
            curr_ord = self._table.horizontalHeader().sortIndicatorOrder()
            if curr_sec == col:
                order = (
                    Qt.SortOrder.AscendingOrder
                    if curr_ord == Qt.SortOrder.DescendingOrder
                    else Qt.SortOrder.DescendingOrder
                )
            else:
                order = Qt.SortOrder.DescendingOrder
            self._table.sortByColumn(col, order)
        else:
            current_order = self._table.horizontalHeader().sortIndicatorOrder()
            self._table.sortByColumn(col, current_order)
        self._last_sort_section = col

    def _set_sort_order(self, order: Qt.SortOrder):
        col = self._table.horizontalHeader().sortIndicatorSection()
        if col < 0:
            col = Col.ADDED
        self._table.sortByColumn(col, order)
        self._last_sort_section = col

    # -- action handlers -----------------------------------------------------

    def _on_add(self):
        dlg = AddDownloadDialog(self, manager=self._manager)
        if dlg.exec() == AddDownloadDialog.DialogCode.Accepted:
            urls = getattr(dlg, "urls", [dlg.url] if dlg.url else [])
            for u in urls:
                self._manager.add_download(
                    u, dlg.save_path, dlg.num_segments
                )

    def _on_add_torrent(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Torrent Files", "",
            "Torrent Files (*.torrent);;All Files (*)",
        )
        for path in paths:
            self._manager.add_download(path)

    def _on_pause(self):
        for did in self._selected_ids():
            self._manager.pause_download(did)

    def _on_pause_all_downloads(self):
        count = self._manager.pause_all_downloads()
        if count > 0:
            self._status_label.setText(f"Paused {count} download{'s' if count > 1 else ''}")
        else:
            self._status_label.setText("No active downloads to pause")

    def _on_resume_all_downloads(self):
        count = self._manager.resume_all_downloads()
        if count > 0:
            self._status_label.setText(f"Resumed {count} download{'s' if count > 1 else ''}")
        else:
            self._status_label.setText("No paused downloads to resume")

    def _on_stop(self):
        for did in self._selected_ids():
            self._manager.stop_download(did)

    def _on_start_seeding(self):
        for did in self._selected_ids():
            self._manager.start_seeding(did)

    def _on_stop_all_seeding(self):
        count = self._manager.stop_all_seeding()
        if count > 0:
            self._status_label.setText(f"Stopped {count} seeding torrent{'s' if count > 1 else ''}")
        else:
            self._status_label.setText("No torrents are currently seeding")

    def _on_resume(self):
        for did in self._selected_ids():
            self._manager.resume_download(did)

    def _on_force_start(self):
        for did in self._selected_ids():
            self._manager.force_start_download(did)

    def _on_delete(self):
        ids = self._selected_ids()
        if not ids:
            return
        dlg = DeleteConfirmDialog(len(ids), self)
        if dlg.exec() == DeleteConfirmDialog.DialogCode.Accepted:
            for did in ids:
                self._manager.delete_download(did, dlg.delete_files)

    def _on_delete_file(self):
        ids = self._selected_ids()
        if not ids:
            return
        items_str = "this file" if len(ids) == 1 else f"the files for these {len(ids)} downloads"
        ans = QMessageBox.question(
            self,
            "Delete File",
            f"Are you sure you want to delete {items_str} from disk (move to Trash)?\n\n"
            "The download entry will be kept, paused, and progress reset to 0.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            for did in ids:
                self._manager.delete_download_file(did)

    def _on_move(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        dlg = MoveDownloadDialog(entry.save_path, self, self._manager._db)
        if dlg.exec() == MoveDownloadDialog.DialogCode.Accepted:
            for did in self._selected_ids():
                self._manager.move_download(did, dlg.new_path)

    def _on_recheck(self):
        for did in self._selected_ids():
            self._manager.recheck_download(did)

    def _on_move_queue_up(self):
        for did in self._selected_ids():
            self._manager.move_queue_up(did)

    def _on_move_queue_down(self):
        for did in reversed(self._selected_ids()):
            self._manager.move_queue_down(did)

    def _on_queue_order_changed(self):
        entries = self._manager.get_all_entries()
        self._model.load_entries(entries)
        self._update_count_label()

    def _on_open_file(self):
        entry = self._first_selected_entry()
        if entry:
            if entry.file_path and Path(entry.file_path).exists():
                os.startfile(entry.file_path)
            else:
                self._manager.mark_file_not_found(entry.id)

    def _apply_table_spans(self):
        self._table.clearSpans()
        if not self._model.is_segregated_view():
            return
        for row in self._model.get_section_header_row_indices():
            self._table.setSpan(row, 0, 1, Col.COUNT)

    def _on_table_clicked(self, index):
        row = index.row()
        if self._model.is_section_header_row(row):
            res = self._model.toggle_section_collapsed(row)
            if res:
                sec_id, is_col = res
                self._manager.db.set_ui_state(f"segregated_{sec_id}_collapsed", is_col)
            self._apply_table_spans()

    def _on_table_double_clicked(self, index):
        if self._model.is_section_header_row(index.row()):
            res = self._model.toggle_section_collapsed(index.row())
            if res:
                sec_id, is_col = res
                self._manager.db.set_ui_state(f"segregated_{sec_id}_collapsed", is_col)
            self._apply_table_spans()
            return
        self._on_open_file()

    def _set_segregation_mode(self, mode: str):
        if mode not in ("status", "date"):
            mode = "status"
        self._segregated_view_mode = mode
        self._manager.db.set_ui_state("segregated_view_mode", mode)
        if hasattr(self, "_act_seg_by_status"):
            self._act_seg_by_status.setChecked(mode == "status")
        if hasattr(self, "_act_seg_by_date"):
            self._act_seg_by_date.setChecked(mode == "date")

        if not self._segregated_view_enabled:
            self._act_segregated_view.setChecked(True)
        else:
            self._model.set_segregated_mode(mode)
            self._apply_table_spans()
            mode_str = "Status" if mode == "status" else "Date"
            self._status_label.setText(f"Segregated view grouped by {mode_str}")

    def _on_toggle_segregated_view(self, checked: bool):
        self._segregated_view_enabled = checked
        self._manager.db.set_ui_state("segregated_view_enabled", checked)
        self._model.set_segregated_view(checked, mode=self._segregated_view_mode)
        self._apply_table_spans()
        mode_str = "Status" if self._segregated_view_mode == "status" else "Date"
        if checked:
            self._status_label.setText(f"Segregated view enabled ({mode_str})")
        else:
            self._status_label.setText("Segregated view disabled")

    def _on_export_selected_csv(self):
        selected_ids = self._selected_ids()
        selected = [self._manager.get_entry(did) for did in selected_ids]
        selected = [e for e in selected if e]
        if not selected:
            QMessageBox.information(
                self,
                "Export Selected to CSV",
                "Please select one or more downloads to export.",
            )
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Selected Downloads as CSV",
            "downloads_export.csv",
            "CSV Files (*.csv);;All Files (*)",
        )
        if not file_path:
            return

        import csv
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Name", "URL/Magnet"])
                for entry in selected:
                    name = DownloadTableModel.get_original_name(entry)
                    writer.writerow([name, entry.url or ""])
            self._status_label.setText(
                f"Exported {len(selected)} download(s) to '{Path(file_path).name}'"
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Export Failed",
                f"Could not export downloads to CSV:\n\n{exc}",
            )

    def _on_open_folder(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        folder = entry.save_path
        file_path = entry.file_path
        if file_path and Path(file_path).exists():
            # Open explorer with file selected
            if sys.platform == "win32":
                subprocess.Popen(
                    ["explorer", "/select,", file_path.replace("/", "\\")]
                )
            else:
                os.startfile(folder)
        elif folder and Path(folder).exists():
            os.startfile(folder)

    def _on_load_backlog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Backlog File", "",
            "Text Files (*.txt);;All Files (*)",
        )
        if path:
            count = self._manager.load_backlog(path)
            self._status_label.setText(
                f"Loaded {count} download(s) from backlog"
            )

    def _on_about(self):
        dlg = QMessageBox(self)
        dlg.setWindowTitle("About My-IDM")
        logo_pm = get_app_logo_pixmap(72)
        if not logo_pm.isNull():
            dlg.setIconPixmap(logo_pm)
        dlg.setText("<h3>My-IDM — Download Manager</h3>")
        dlg.setInformativeText(
            "<p><b>Version 1.0.0</b></p>"
            "<p>A high-speed download manager featuring multi-segment parallel HTTP "
            "downloading, full BitTorrent swarm engine, VPN Kill Switch privacy protection, "
            "and automated virus & malware inspection.</p>"
            "<p>Built with Python, PySide6, asyncio/aiohttp, and libtorrent.</p>"
            "<p style='color: #8fa0b5; margin-top: 8px;'>© Rakesh Malik, 2026</p>"
        )
        dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
        dlg.exec()

    def _on_copy_url(self):
        ids = self._selected_ids()
        if not ids:
            return
        urls: list[str] = []
        for did in ids:
            entry = self._manager.get_entry(did)
            if entry and entry.url:
                urls.append(entry.url)
        if urls:
            clipboard = QGuiApplication.clipboard()
            if clipboard:
                clipboard.setText("\n".join(urls))
                if len(urls) == 1:
                    kind = "Magnet link" if urls[0].startswith("magnet:") else "URL"
                    self._status_label.setText(f"Copied {kind} to clipboard")
                else:
                    self._status_label.setText(f"Copied {len(urls)} URLs/Magnets to clipboard")

    def _on_rename(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        current_name = entry.filename or (os.path.basename(entry.file_path) if entry.file_path else "")
        dlg = RenameDialog(current_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_name = dlg.new_name.strip()
            if new_name and new_name != current_name:
                success, err = self._manager.rename_download(entry.id, new_name)
                if not success:
                    QMessageBox.warning(
                        self,
                        "Rename Failed",
                        f"Could not rename '{current_name}':\n\n{err}",
                    )
                else:
                    self._status_label.setText(f"Renamed to '{new_name}'")

    # -- context menu --------------------------------------------------------

    def _show_context_menu(self, pos):
        idx = self._table.indexAt(pos)
        if idx.isValid():
            if self._model.is_section_header_row(idx.row()):
                sec_menu = QMenu(self)
                entry = self._model._entries[idx.row()]
                action_word = "Expand" if entry.section_collapsed else "Collapse"
                act_this = QAction(f"{action_word} '{entry.section_title}' Section", self)
                def _toggle_this():
                    res = self._model.toggle_section_collapsed(idx.row())
                    if res:
                        sec_id, is_col = res
                        self._manager.db.set_ui_state(f"segregated_{sec_id}_collapsed", is_col)
                    self._apply_table_spans()
                act_this.triggered.connect(_toggle_this)
                sec_menu.addAction(act_this)
                sec_menu.addSeparator()
                current_mode = self._model.segregated_mode()
                if current_mode == "status":
                    active_sec_ids = ("active", "seeding", "inactive")
                else:
                    active_sec_ids = (
                        "date_today", "date_yesterday", "date_last_7_days", "date_last_30_days", "date_older"
                    )

                act_expand_all = QAction("Expand All Sections", self)
                def _expand_all():
                    for sid in active_sec_ids:
                        self._model.set_section_collapsed(sid, False)
                        self._manager.db.set_ui_state(f"segregated_{sid}_collapsed", False)
                    self._apply_table_spans()
                act_expand_all.triggered.connect(_expand_all)
                sec_menu.addAction(act_expand_all)

                act_collapse_all = QAction("Collapse All Sections", self)
                def _collapse_all():
                    for sid in active_sec_ids:
                        self._model.set_section_collapsed(sid, True)
                        self._manager.db.set_ui_state(f"segregated_{sid}_collapsed", True)
                    self._apply_table_spans()
                act_collapse_all.triggered.connect(_collapse_all)
                sec_menu.addAction(act_collapse_all)

                sec_menu.addSeparator()
                if current_mode == "status":
                    act_switch = QAction("Switch to Date Grouping", self)
                    act_switch.triggered.connect(lambda: self._set_segregation_mode("date"))
                else:
                    act_switch = QAction("Switch to Status Grouping", self)
                    act_switch.triggered.connect(lambda: self._set_segregation_mode("status"))
                sec_menu.addAction(act_switch)

                sec_menu.exec(self._table.viewport().mapToGlobal(pos))
                return

            sm = self._table.selectionModel()
            selected_rows = {i.row() for i in sm.selectedRows()}
            if idx.row() not in selected_rows:
                self._table.selectRow(idx.row())
        else:
            return

        menu = QMenu(self)
        menu.addAction(self._act_resume)
        menu.addAction(self._act_force_start)
        menu.addAction(self._act_pause)
        menu.addAction(self._act_stop)
        menu.addAction(self._act_start_seeding)
        menu.addSeparator()
        menu.addAction(self._act_move_up)
        menu.addAction(self._act_move_down)
        menu.addSeparator()
        menu.addAction(self._act_copy_url)
        menu.addAction(self._act_rename)
        menu.addAction(self._act_export_csv)
        menu.addSeparator()
        menu.addAction(self._act_scan_antivirus)
        menu.addAction(self._act_recheck)
        menu.addAction(self._act_move)
        menu.addSeparator()
        menu.addAction(self._act_toggle_details)
        menu.addSeparator()
        # Bandwidth Allocation Submenu
        bw_menu = menu.addMenu("Bandwidth Allocation")
        entry = self._first_selected_entry()
        curr_alloc = (entry.metadata.get("bandwidth_allocation", "max") if entry and entry.metadata else "max").lower()
        alloc_options = [
            ("Low (25%)", "low"),
            ("Medium (50%)", "medium"),
            ("High (75%)", "high"),
            ("Max (100%)", "max"),
        ]
        for label, val in alloc_options:
            act = bw_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(curr_alloc == val)
            act.triggered.connect(lambda checked=False, a=val: self._on_set_bandwidth_allocation(a))
        menu.addSeparator()
        menu.addAction(self._act_open_file)
        menu.addAction(self._act_open_folder)
        menu.addSeparator()
        menu.addAction(self._act_delete_file)
        menu.addAction(self._act_delete)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_set_bandwidth_allocation(self, allocation: str):
        for did in self._selected_ids():
            self._manager.set_download_bandwidth_allocation(did, allocation)
        self._table.viewport().update()

    # -- signal handlers from manager ----------------------------------------

    def _on_progress_updated(self, download_id: str, downloaded: int,
                             total: int, speed: float, eta: float,
                             seeds: int, peers: int, upload_speed: float):
        self._model.update_progress(
            download_id, downloaded, total, speed, eta,
            seeds, peers, upload_speed,
        )
        self._update_speed_label()
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

    def _on_status_changed(self, download_id: str, status: str,
                           error_msg: str):
        self._model.update_status(download_id, status, error_msg)
        self._update_count_label()
        self._update_speed_label()
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

        if status == "completed":
            if self._manager.general_config.notify_on_completion:
                if download_id not in self._completed_notified:
                    self._completed_notified.add(download_id)
                    entry = self._manager.get_entry(download_id)
                    fname = entry.filename if entry and entry.filename else download_id
                    from my_idm.notifications import notify_download_complete
                    notify_download_complete(fname)
        elif status in ("downloading", "queued", "paused"):
            self._completed_notified.discard(download_id)

    def _on_filename_resolved(self, download_id: str, filename: str):
        self._model.update_filename(download_id, filename)
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

    def _on_download_added(self, download_id: str):
        entry = self._manager.get_entry(download_id)
        if entry:
            self._model.add_entry(entry)
            self._update_count_label()

    def _on_download_removed(self, download_id: str):
        self._model.remove_entry(download_id)
        self._update_count_label()
        self._update_speed_label()

    def _on_download_moved(self, download_id: str):
        entry = self._manager.get_entry(download_id)
        if entry:
            self._model.refresh_entry(download_id, entry)

    def _on_download_renamed(self, download_id: str, new_filename: str):
        entry = self._manager.get_entry(download_id)
        if entry:
            entry.filename = new_filename
            self._model.refresh_entry(download_id, entry)
        else:
            self._model.rename_entry(download_id, new_filename)
        self._table.viewport().update()
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

    # -- helpers -------------------------------------------------------------

    def _update_count_label(self):
        total_all = self._model.total_unfiltered_count()
        visible = self._model.visible_download_count()
        active = sum(
            1 for e in self._model.all_entries
            if e.status in ("downloading", "checking", "fetching_metadata")
        )
        if self._model.is_filtered():
            self._count_label.setText(f"{visible} of {total_all} Downloads, {active} Active (Filtered)")
        else:
            self._count_label.setText(f"{total_all} Downloads, {active} Active")

    def _update_speed_label(self):
        down, up = self._model.get_aggregate_speeds()
        from my_idm.download_model import _format_speed
        net_cfg = self._manager.network_config
        dl_lim = net_cfg.download_limit or 0
        ul_lim = net_cfg.upload_limit or 0
        dl_tag = f" [Limit: {_format_speed(dl_lim)}]" if dl_lim > 0 else ""
        ul_tag = f" [Limit: {_format_speed(ul_lim)}]" if ul_lim > 0 else ""
        self._speed_label.setText(f"↓ {_format_speed(down)}{dl_tag}  ↑ {_format_speed(up)}{ul_tag}")
        self._speed_label.setToolTip(
            f"Total Transfer Speed\n"
            f"Download Limit: {_format_speed(dl_lim) if dl_lim > 0 else 'Unlimited'}\n"
            f"Upload Limit: {_format_speed(ul_lim) if ul_lim > 0 else 'Unlimited'}\n"
            f"Click or right-click to adjust limits"
        )

    def _on_bandwidth_limits_changed(self, download_limit: int, upload_limit: int):
        self._update_speed_label()

    def _show_speed_context_menu(self, pos):
        menu = QMenu(self)
        from my_idm.download_model import _format_speed
        net_cfg = self._manager.network_config
        curr_dl = net_cfg.download_limit or 0
        curr_ul = net_cfg.upload_limit or 0

        # Submenu for Download Speed Limit
        dl_menu = menu.addMenu("Download Speed Limit")
        preset_values = {val for _, val in SPEED_LIMIT_PRESETS}
        for label, val in SPEED_LIMIT_PRESETS:
            act = dl_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(curr_dl == val)
            act.triggered.connect(lambda checked=False, v=val: self._set_speed_limit(v, is_upload=False))

        dl_menu.addSeparator()
        custom_dl_act = dl_menu.addAction(
            f"Custom ({_format_speed(curr_dl)})..." if (curr_dl > 0 and curr_dl not in preset_values) else "Custom..."
        )
        custom_dl_act.setCheckable(True)
        custom_dl_act.setChecked(curr_dl > 0 and curr_dl not in preset_values)
        custom_dl_act.triggered.connect(lambda: self._prompt_custom_speed_limit(is_upload=False))

        # Submenu for Upload Speed Limit
        ul_menu = menu.addMenu("Upload Speed Limit")
        for label, val in SPEED_LIMIT_PRESETS:
            act = ul_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(curr_ul == val)
            act.triggered.connect(lambda checked=False, v=val: self._set_speed_limit(v, is_upload=True))

        ul_menu.addSeparator()
        custom_ul_act = ul_menu.addAction(
            f"Custom ({_format_speed(curr_ul)})..." if (curr_ul > 0 and curr_ul not in preset_values) else "Custom..."
        )
        custom_ul_act.setCheckable(True)
        custom_ul_act.setChecked(curr_ul > 0 and curr_ul not in preset_values)
        custom_ul_act.triggered.connect(lambda: self._prompt_custom_speed_limit(is_upload=True))

        menu.exec(self._speed_label.mapToGlobal(pos))

    def _set_speed_limit(self, limit: int, is_upload: bool):
        net_cfg = self._manager.network_config
        dl = net_cfg.download_limit if is_upload else limit
        ul = limit if is_upload else net_cfg.upload_limit
        self._manager.set_bandwidth_limits(dl, ul)
        self._update_speed_label()

    def _prompt_custom_speed_limit(self, is_upload: bool):
        net_cfg = self._manager.network_config
        curr = (net_cfg.upload_limit if is_upload else net_cfg.download_limit) or 0
        kind = "Upload" if is_upload else "Download"
        val_kb, ok = QInputDialog.getInt(
            self,
            f"Custom {kind} Speed Limit",
            f"Enter {kind.lower()} speed limit in KB/s (0 = Unlimited):",
            value=curr // 1024,
            minValue=0,
            maxValue=10_000_000,
            step=10,
        )
        if ok:
            self._set_speed_limit(val_kb * 1024, is_upload)

    def _on_open_preferences(self, initial_tab: int = 0):
        dlg = SettingsDialog(
            general_config=self._manager.general_config,
            torrent_config=self._manager.torrent_config,
            network_config=self._manager.network_config,
            security_config=self._manager.security_config,
            tor_config=self._manager.tor_config,
            external_tools_config=self._manager.external_tools_config,
            browser_config=self._manager.browser_config,
            db=self._manager._db,
            parent=self,
            initial_tab=initial_tab,
        )
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._manager.set_general_config(dlg.general_config)
            self._manager.set_torrent_config(dlg.torrent_config)
            self._manager.set_network_config(dlg.network_config)
            self._manager.set_security_config(dlg.security_config)
            self._manager.set_external_tools_config(dlg.external_tools_config)
            self._manager.set_browser_config(dlg.browser_config)
            old_tor_enabled = self._manager.tor_config.enabled
            self._manager.set_tor_config(dlg.tor_config)
            if dlg.tor_config.enabled != old_tor_enabled:
                self._on_toggle_tor(dlg.tor_config.enabled)
            if self._tray_icon:
                if dlg.general_config.enable_system_tray:
                    self._tray_icon.show()
                else:
                    self._tray_icon.hide()

    def _on_open_torrent_settings(self):
        self._on_open_preferences(1)

    def _on_open_browser_settings(self):
        self._on_open_preferences(2)

    def _on_open_network_settings(self):
        self._on_open_preferences(3)

    def _on_open_tor_settings(self):
        self._on_open_preferences(3)

    def _on_open_security_settings(self):
        self._on_open_preferences(4)

    def _on_open_external_tools_settings(self):
        self._on_open_preferences(5)

    def _on_launch_animepahe_gui(self):
        cfg = self._manager.external_tools_config
        repo = cfg.get_effective_repo_path()
        if not repo or not os.path.isdir(repo):
            res = QMessageBox.question(
                self,
                "AnimePahe Not Configured",
                "The AnimePahe repository folder is not configured or does not exist.\n\n"
                "Would you like to configure the repository location in Preferences now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if res == QMessageBox.StandardButton.Yes:
                self._on_open_external_tools_settings()
            return

        ok, msg = launch_animepahe_gui(cfg)
        if ok:
            self._status_label.setText("Launched AnimePahe Downloader GUI")
        else:
            QMessageBox.warning(self, "Failed to Launch AnimePahe GUI", msg)

    def _on_start_animepahe_cli(self):
        cfg = self._manager.external_tools_config
        repo = cfg.get_effective_repo_path()
        if not repo or not os.path.isdir(repo):
            res = QMessageBox.question(
                self,
                "AnimePahe Not Configured",
                "The AnimePahe repository folder is not configured or does not exist.\n\n"
                "Would you like to configure the repository location in Preferences now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if res == QMessageBox.StandardButton.Yes:
                self._on_open_external_tools_settings()
            return

        ok, msg = self._manager.start_animepahe_scraper()
        self._status_label.setText(msg)
        if not ok:
            QMessageBox.warning(self, "Failed to Start Scraper", msg)

    def _on_toggle_details_btn_clicked(self):
        if self._details_panel.isVisible() and self._details_panel.current_mode() == "details":
            self._act_toggle_details.setChecked(False)
        else:
            self._details_panel.set_mode("details")
            self._act_toggle_details.setChecked(True)

    def _on_toggle_console_btn_clicked(self):
        if self._details_panel.isVisible() and self._details_panel.current_mode() == "console":
            self._act_toggle_details.setChecked(False)
        else:
            self._details_panel.set_mode("console")
            self._act_toggle_details.setChecked(True)

    def _on_view_animepahe_console_log(self):
        self._on_toggle_console_btn_clicked()

    def _footer_toggle_style(self, active: bool) -> str:
        if active:
            return """
                QPushButton {
                    background: rgba(88, 166, 255, 0.15);
                    color: #58a6ff;
                    border: 1px solid #58a6ff;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: rgba(88, 166, 255, 0.25);
                    border-color: #79c0ff;
                    color: #79c0ff;
                }
            """
        return """
            QPushButton {
                background: transparent;
                color: #8892b0;
                border: 1px solid #3b4252;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
            }
            QPushButton:hover {
                background: #2e3440;
                color: #d8dee9;
            }
        """

    def _sync_panel_buttons(self):
        is_vis = getattr(self, "_act_toggle_details", None) is not None and self._act_toggle_details.isChecked() and not self._details_panel.isHidden()
        is_console = (self._details_panel.current_mode() == "console")
        details_on = is_vis and not is_console
        console_on = is_vis and is_console

        if hasattr(self, "_details_status_btn"):
            self._details_status_btn.setText("📋 Details: ON" if details_on else "📋 Details: OFF")
            self._details_status_btn.setStyleSheet(self._footer_toggle_style(details_on))

        if hasattr(self, "_console_status_btn"):
            self._console_status_btn.setText("📄 Console: ON" if console_on else "📄 Console: OFF")
            self._console_status_btn.setStyleSheet(self._footer_toggle_style(console_on))

    def _on_open_animepahe_console_log_file(self):
        from my_idm.external_tools import open_file_in_default_app
        log_path = self._manager.external_tools_config.get_console_log_path()
        ok, msg = open_file_in_default_app(log_path, create_if_missing=True)
        if not ok:
            QMessageBox.warning(self, "Cannot Open Console Log", msg)

    def _on_view_animepahe_debug_log(self):
        from my_idm.external_tools import open_file_in_default_app
        log_path = self._manager.external_tools_config.get_debug_log_path()
        ok, msg = open_file_in_default_app(log_path, create_if_missing=True)
        if not ok:
            QMessageBox.warning(self, "Cannot Open Debug Log", msg)

    def _on_animepahe_status_changed(self, is_running: bool):
        self._animepahe_status_btn.setVisible(is_running)
        if is_running:
            q_len = getattr(self._manager, "get_animepahe_queue_length", lambda: 0)()
            if q_len > 0:
                self._animepahe_status_btn.setText(f"🎬 AnimePahe: Active (+{q_len} queued)")
            else:
                self._animepahe_status_btn.setText("🎬 AnimePahe: Active")

    def _on_animepahe_queue_changed(self, queue_len: int):
        if self._animepahe_status_btn.isVisible():
            if queue_len > 0:
                self._animepahe_status_btn.setText(f"🎬 AnimePahe: Active (+{queue_len} queued)")
            else:
                self._animepahe_status_btn.setText("🎬 AnimePahe: Active")

    def _show_animepahe_status_menu(self):
        menu = QMenu(self)

        act_console = QAction(_create_emoji_icon("📄"), "View Console Logs (Bottom Panel)", self)
        act_console.triggered.connect(self._on_view_animepahe_console_log)
        menu.addAction(act_console)

        act_file = QAction(_create_emoji_icon("↗️"), "Open Console Log in Editor…", self)
        act_file.triggered.connect(self._on_open_animepahe_console_log_file)
        menu.addAction(act_file)

        act_debug = QAction(_create_emoji_icon("🔍"), "View Debug Logs…", self)
        act_debug.triggered.connect(self._on_view_animepahe_debug_log)
        menu.addAction(act_debug)

        menu.addSeparator()

        act_gui = QAction(_create_emoji_icon("🎬"), "Launch AnimePahe GUI…", self)
        act_gui.triggered.connect(self._on_launch_animepahe_gui)
        menu.addAction(act_gui)

        if self._manager.is_animepahe_running():
            q_len = getattr(self._manager, "get_animepahe_queue_length", lambda: 0)()
            if q_len > 0:
                act_info = QAction(f"📋 Pending in Queue: {q_len} task(s)", self)
                act_info.setEnabled(False)
                menu.addAction(act_info)

            stop_label = f"Stop Scraper & Clear Queue ({q_len})" if q_len > 0 else "Stop Background Scraper"
            act_stop = QAction(_create_emoji_icon("⏹️"), stop_label, self)
            def _stop():
                ok, msg = self._manager.stop_animepahe_scraper()
                self._status_label.setText(msg)
            act_stop.triggered.connect(_stop)
            menu.addAction(act_stop)
        else:
            act_start = QAction(_create_emoji_icon("▶️"), "Start Background Scraper (CLI)", self)
            act_start.triggered.connect(self._on_start_animepahe_cli)
            menu.addAction(act_start)

        menu.addSeparator()
        act_settings = QAction(_create_emoji_icon("🛠️"), "External Tools Settings…", self)
        act_settings.triggered.connect(self._on_open_external_tools_settings)
        menu.addAction(act_settings)

        btn_pos = self._animepahe_status_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height()))
        menu.exec(btn_pos)

    def _on_scan_selected_file(self):
        for did in self._selected_ids():
            self._manager.scan_download_file(did)

    def _on_threat_detected(self, download_id: str, report: str):
        entry = self._manager.get_entry(download_id)
        name = entry.filename if entry else download_id
        QMessageBox.critical(
            self,
            "⚠️ Malware / Threat Detected!",
            f"Antivirus scanning detected a potential threat in:\n\n"
            f"File: {name}\n\n"
            f"Details:\n{report}",
        )

    def _update_network_status_badge(self, config: NetworkConfig):
        if config.is_interface_bound:
            is_vpn = is_vpn_adapter_name(config.interface_name)
            icon = "🛡️ VPN" if is_vpn else "🌐 Net"
            ks = " [🔒 KS]" if config.kill_switch else ""
            self._vpn_status_btn.setText(f"{icon}: {config.interface_name}{ks}")
            self._vpn_status_btn.setToolTip(
                f"Bound to {config.interface_name} ({config.interface_ip})\n"
                f"Kill Switch: {'Enabled' if config.kill_switch else 'Disabled'}\n"
                "Click to configure VPN & Network Settings"
            )
            bg = "#193524" if is_vpn else "#222d3d"
            fg = "#50fa7b" if is_vpn else "#8be9fd"
            border = "#50fa7b" if is_vpn else "#3d4b60"
            self._vpn_status_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    color: {fg};
                    border: 1px solid {border};
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background: #2a4030;
                }}
            """)
        elif config.proxy_enabled and config.proxy_host:
            self._vpn_status_btn.setText(f"🌐 Proxy: {config.proxy_type.upper()}")
            self._vpn_status_btn.setToolTip(
                f"Proxy: {config.proxy_url}\nClick to configure VPN & Network Settings"
            )
            self._vpn_status_btn.setStyleSheet("""
                QPushButton {
                    background: #222d3d;
                    color: #f1fa8c;
                    border: 1px solid #6272a4;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: #2d3b50;
                }
            """)
        else:
            self._vpn_status_btn.setText("🌐 Net: Default")
            self._vpn_status_btn.setToolTip(
                "Using system default routing.\nClick to configure VPN & Network Settings"
            )
            self._vpn_status_btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #8892b0;
                    border: 1px solid #3b4252;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background: #2e3440;
                    color: #d8dee9;
                }
            """)

    def _on_toggle_tor(self, checked: bool):
        self._on_tor_status_changed("connecting" if checked else "disconnecting", "")
        QApplication.processEvents()
        success, msg = self._manager.toggle_tor(checked)
        self._status_label.setText(msg)
        self._model.set_tor_config(self._manager.tor_config)
        self._table.viewport().update()
        if not success and checked:
            self._act_tor.blockSignals(True)
            self._act_tor.setChecked(False)
            self._act_tor.blockSignals(False)
            self._on_tor_status_changed("error", msg)

            QMessageBox.critical(
                self,
                "⚠️ Tor Connection Error",
                f"Unable to activate Tor network privacy:\n\n{msg}\n\n"
                "Please verify that Tor or Tor Browser is installed, or configure the path in Tools → Tor Network Settings.",
            )
        else:
            self._on_tor_status_changed("connected" if checked else "disconnected", msg)

    def _on_tor_status_changed(self, state: str, message: str):
        if state == "connecting":
            self._tor_toolbar_progress.setVisible(True)
            self._tor_toolbar_progress.setRange(0, 0)
            self._tor_footer_progress.setVisible(True)
            self._tor_footer_progress.setRange(0, 0)
            self._tor_status_btn.setText("🧅 Tor: Connecting...")
            self._act_tor.setText("Tor: Connecting...")
            self._apply_tor_transition_style()
        elif state == "disconnecting":
            self._tor_toolbar_progress.setVisible(True)
            self._tor_toolbar_progress.setRange(0, 0)
            self._tor_footer_progress.setVisible(True)
            self._tor_footer_progress.setRange(0, 0)
            self._tor_status_btn.setText("🧅 Tor: Disconnecting...")
            self._act_tor.setText("Tor: Disconnecting...")
            self._apply_tor_transition_style()
        elif state in ("connected", "disconnected", "error"):
            self._tor_toolbar_progress.setVisible(False)
            self._tor_footer_progress.setVisible(False)
            self._on_tor_config_changed(self._manager.tor_config)

    def _apply_tor_transition_style(self):
        trans_btn_style = """
            QPushButton {
                background: #2a2818;
                color: #f1fa8c;
                border: 1px solid #f1fa8c;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #383520;
            }
        """
        self._tor_status_btn.setStyleSheet(trans_btn_style)

        trans_tb_style = """
            QToolButton {
                background: #2a2818;
                color: #f1fa8c;
                border: 1px solid #f1fa8c;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 12px;
                font-weight: bold;
            }
            QToolButton:hover {
                background: #383520;
            }
        """
        if hasattr(self, "_tor_toolbar_btn"):
            self._tor_toolbar_btn.setStyleSheet(trans_tb_style)

    def _on_tor_config_changed(self, config: TorConfig):
        self._model.set_tor_config(config)
        self._table.viewport().update()

        self._act_tor.blockSignals(True)
        self._act_tor.setChecked(config.enabled)
        self._act_tor.blockSignals(False)

        self._tor_toolbar_progress.setVisible(False)
        self._tor_footer_progress.setVisible(False)

        if config.enabled:
            self._act_tor.setText("Tor: ON")
            routed = []
            if config.route_http:
                routed.append("HTTP")
            if config.route_torrent:
                routed.append("Torrent")
            traffic_str = ", ".join(routed) if routed else "None"
            self._act_tor.setToolTip(
                f"Tor is ON ({config.socks5_url})\n"
                f"Routing: {traffic_str}\n"
                "Click to disable Tor routing"
            )
            self._tor_status_btn.setText(f"🧅 Tor: {traffic_str}")
            self._tor_status_btn.setToolTip(
                f"Tor active on {config.proxy_host}:{config.proxy_port}\n"
                f"Traffic routed: {traffic_str}\n"
                "Click to open Tor Settings"
            )
            # Green styling when ON
            green_btn_style = """
                QPushButton {
                    background: #193524;
                    color: #50fa7b;
                    border: 1px solid #50fa7b;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: #2a4e34;
                }
            """
            self._tor_status_btn.setStyleSheet(green_btn_style)

            green_tb_style = """
                QToolButton {
                    background: #193524;
                    color: #50fa7b;
                    border: 1px solid #50fa7b;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 12px;
                    font-weight: bold;
                }
                QToolButton:hover {
                    background: #2a4e34;
                }
            """
            if hasattr(self, "_tor_toolbar_btn"):
                self._tor_toolbar_btn.setStyleSheet(green_tb_style)
        else:
            self._act_tor.setText("Tor: OFF")
            self._act_tor.setToolTip(
                "Tor is OFF\nClick to enable Tor routing"
            )
            self._tor_status_btn.setText("🧅 Tor: OFF")
            self._tor_status_btn.setToolTip(
                "Tor is disabled.\nClick to open Tor Settings"
            )
            off_btn_style = """
                QPushButton {
                    background: transparent;
                    color: #6272a4;
                    border: 1px solid #3b4252;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background: #2e3440;
                    color: #d8dee9;
                }
            """
            self._tor_status_btn.setStyleSheet(off_btn_style)

            off_tb_style = """
                QToolButton {
                    background: transparent;
                    color: #8892b0;
                    border: 1px solid #3b4252;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 12px;
                }
                QToolButton:hover {
                    background: #2e3440;
                    color: #d8dee9;
                }
            """
            if hasattr(self, "_tor_toolbar_btn"):
                self._tor_toolbar_btn.setStyleSheet(off_tb_style)

    # -- details panel handlers ----------------------------------------------

    def _on_toggle_details(self, checked: bool):
        if checked:
            self._details_panel.setVisible(True)
            sizes = self._splitter.sizes()
            total = sum(sizes) if sum(sizes) > 250 else (self.height() or 700)
            bot_h = min(total - 100, max(140, getattr(self, "_details_height", 250)))
            top_h = max(100, total - bot_h)
            self._splitter.setSizes([top_h, bot_h])
            entry = self._first_selected_entry()
            self._details_panel.set_download_id(entry.id if entry else None)
        else:
            sizes = self._splitter.sizes()
            if len(sizes) == 2 and sizes[1] >= 50:
                self._details_height = sizes[1]
            self._details_panel.setVisible(False)
        tip = (
            "Hide bottom panel (F4)"
            if checked
            else "Show bottom panel (F4)"
        )
        self._act_toggle_details.setToolTip(tip)
        self._sync_panel_buttons()

    def _on_reset_view(self):
        """Reset all table/grid settings to defaults: column visibility, filters, sorting, and column widths."""
        # 1. Reset column widths to defaults
        self._table.setColumnWidth(Col.QUEUE, 45)
        self._table.setColumnWidth(Col.NAME, 270)
        self._table.setColumnWidth(Col.SOURCE_DOMAIN, 160)
        self._table.setColumnWidth(Col.SIZE, 90)
        self._table.setColumnWidth(Col.PROGRESS, 160)
        self._table.setColumnWidth(Col.STATUS, 135)
        self._table.setColumnWidth(Col.SPEED, 110)
        self._table.setColumnWidth(Col.ETA, 80)
        self._table.setColumnWidth(Col.SEEDS_PEERS, 100)
        self._table.setColumnWidth(Col.ADDED, 130)
        self._table.setColumnWidth(Col.LAST_TRIED, 130)
        self._table.setColumnWidth(Col.COMPLETED, 130)
        self._table.setColumnWidth(Col.SAVE_PATH, 220)
        self._table.setColumnWidth(Col.FILE_NAME, 220)

        # 2. Show all columns (reset column visibility)
        header = self._header_view
        for col in range(Col.COUNT):
            header.setSectionHidden(col, False)

        # 3. Reset column order to default with Source Domain and File / Folder Name at the end
        for visual in range(Col.COUNT - 1, -1, -1):
            logical = header.logicalIndex(visual)
            if logical != visual:
                header.moveSection(visual, logical)
        header.moveSection(header.visualIndex(Col.SOURCE_DOMAIN), Col.COUNT - 2)
        header.moveSection(header.visualIndex(Col.FILE_NAME), Col.COUNT - 1)

        # 4. Clear all filters (status and type filters)
        self._model.clear_filters()

        # 5. Reset sorting to default: Date Added, Descending
        self._table.sortByColumn(Col.ADDED, Qt.SortOrder.DescendingOrder)
        self._last_sort_section = Col.ADDED

        # 6. Reset row height to default
        self._table.verticalHeader().setDefaultSectionSize(36)

        # 7. Update UI state in database
        self._save_ui_state_to_db()

        self._status_label.setText("View reset to defaults")

    def _on_table_selection_changed(self, *args):
        entry = self._first_selected_entry()
        self._details_panel.set_download_id(entry.id if entry else None)

    def _on_details_timer_tick(self):
        if self._details_panel.isVisible() and self._details_panel.current_download_id:
            self._details_panel.refresh()
        self._update_speed_label()

    # -- UI State persistence in database ------------------------------------

    def _save_ui_state_to_db(self):
        """Save window size, location, maximized or not, column lengths, splitter state in DB."""
        try:
            is_max = self.isMaximized()
            # If currently maximized, normalGeometry gives the un-maximized rect for restoring size & location
            geom = self.normalGeometry() if is_max else self.geometry()

            col_widths = {
                str(col): self._table.columnWidth(col)
                for col in range(Col.COUNT)
            }
            header_hex = bytes(self._table.horizontalHeader().saveState().toHex()).decode()
            splitter_hex = bytes(self._splitter.saveState().toHex()).decode()
            sizes = self._splitter.sizes()
            details_vis = self._details_panel.isVisible()

            if details_vis and len(sizes) == 2 and sizes[1] >= 50:
                self._details_height = sizes[1]

            total = sum(sizes) if sum(sizes) > 250 else 700
            safe_bot = min(total - 100, max(140, getattr(self, "_details_height", 250)))
            splitter_sizes = [max(100, total - safe_bot), safe_bot]

            details_state = self._details_panel.get_state()

            sort_sec = self._table.horizontalHeader().sortIndicatorSection()
            try:
                sort_ord = int(self._table.horizontalHeader().sortIndicatorOrder().value)
            except (AttributeError, TypeError, ValueError):
                sort_ord = int(Qt.SortOrder.DescendingOrder.value)

            if sort_sec < 0:
                sort_sec = Col.ADDED
                sort_ord = int(Qt.SortOrder.DescendingOrder.value)

            window_geom_hex = bytes(self.saveGeometry().toHex()).decode()

            state = {
                "window_geometry": window_geom_hex,
                "x": geom.x() if is_max else self.x(),
                "y": geom.y() if is_max else self.y(),
                "width": geom.width(),
                "height": geom.height(),
                "is_maximized": is_max,
                "column_widths": col_widths,
                "header_state": header_hex,
                "splitter_state": splitter_hex,
                "splitter_sizes": splitter_sizes,
                "details_visible": details_vis,
                "details_height": safe_bot,
                "details_state": details_state,
                "sort_column": sort_sec,
                "sort_order": sort_ord,
            }
            self._manager.save_ui_state(state)

            # Also sync to QSettings for backwards compatibility
            settings = QSettings("MyIDM", "My-IDM")
            settings.setValue("header_state", self._table.horizontalHeader().saveState())
            settings.setValue("splitter_state", self._splitter.saveState())
            settings.setValue("details_visible", details_vis)
            settings.setValue("details_height", safe_bot)
            settings.setValue("details_tab", details_state.get("current_tab", 0))
        except Exception as exc:
            log.warning("Failed to save window state to DB: %s", exc)

    def _restore_ui_state_from_db(self):
        """Restore window size, location, maximized or not, column lengths, splitter state from DB."""
        try:
            state = self._manager.get_ui_state()
            if not state:
                # Fallback to legacy QSettings if DB has no stored state
                settings = QSettings("MyIDM", "My-IDM")
                header_state = settings.value("header_state")
                if header_state:
                    self._table.horizontalHeader().restoreState(header_state)
                splitter_state = settings.value("splitter_state")
                if splitter_state:
                    self._splitter.restoreState(splitter_state)

                details_h = settings.value("details_height")
                if details_h is not None:
                    try:
                        self._details_height = max(140, int(details_h))
                    except (ValueError, TypeError):
                        self._details_height = 250
                else:
                    self._details_height = 250

                details_tab = settings.value("details_tab")
                if details_tab is not None:
                    self._details_panel.restore_state({"current_tab": details_tab})

                details_vis = settings.value("details_visible")
                if details_vis is not None:
                    is_vis = bool(details_vis)
                else:
                    is_vis = True

                self._act_toggle_details.setChecked(is_vis)
                self._details_panel.setVisible(is_vis)

                sizes = self._splitter.sizes()
                total = sum(sizes) if sum(sizes) > 250 else 700
                safe_bot = min(total - 100, max(140, self._details_height))
                safe_top = max(100, total - safe_bot)
                if is_vis:
                    self._splitter.setSizes([safe_top, safe_bot])
                else:
                    self._splitter.setSizes([total, 0])

                tip = (
                    "Hide bottom panel (F4)"
                    if is_vis
                    else "Show bottom panel (F4)"
                )
                self._act_toggle_details.setToolTip(tip)
                self._sync_panel_buttons()

                self._table.horizontalHeader().setSectionsMovable(True)
                self._table.horizontalHeader().setFirstSectionMovable(True)
                self._table.horizontalHeader().setStretchLastSection(False)
                self._table.horizontalHeader().setCascadingSectionResizes(False)
                self._table.sortByColumn(Col.ADDED, Qt.SortOrder.DescendingOrder)
                self._last_sort_section = Col.ADDED
                return

            # Window size & location
            window_geom = state.get("window_geometry")
            if window_geom:
                try:
                    self.restoreGeometry(QByteArray.fromHex(window_geom.encode()))
                except Exception:
                    pass
            else:
                w = state.get("width")
                h = state.get("height")
                x = state.get("x")
                y = state.get("y")
                has_valid_size = w and h and w >= 400 and h >= 300
                has_valid_pos = x is not None and y is not None
                if has_valid_size:
                    self.resize(int(w), int(h))
                if has_valid_pos:
                    self.move(int(x), int(y))
                if state.get("is_maximized"):
                    self.showMaximized()

            # Column lengths / widths
            col_widths = state.get("column_widths")
            if col_widths and isinstance(col_widths, dict):
                for col_str, width in col_widths.items():
                    try:
                        self._table.setColumnWidth(int(col_str), int(width))
                    except (ValueError, TypeError):
                        pass

            # Header state (order, sorting indicator)
            header_hex = state.get("header_state")
            if header_hex:
                try:
                    self._table.horizontalHeader().restoreState(
                        QByteArray.fromHex(header_hex.encode())
                    )
                except Exception:
                    pass

            # Ensure sections remain movable and flags are not overwritten by saved state
            header = self._table.horizontalHeader()
            header.setSectionsMovable(True)
            header.setFirstSectionMovable(True)
            header.setStretchLastSection(False)
            header.setCascadingSectionResizes(False)

            # Ensure Col.FILE_NAME is visible and properly sized if restored from an older state
            header.setSectionHidden(Col.FILE_NAME, False)
            if self._table.columnWidth(Col.FILE_NAME) < 50:
                self._table.setColumnWidth(Col.FILE_NAME, 220)

            # Restore sort column & order: default to Col.ADDED, DescendingOrder
            sort_col = state.get("sort_column")
            sort_ord = state.get("sort_order")
            if sort_col is None or sort_col < 0:
                sort_col = Col.ADDED
                sort_ord = int(Qt.SortOrder.DescendingOrder.value)
            try:
                order = Qt.SortOrder(sort_ord)
            except (ValueError, TypeError):
                order = Qt.SortOrder.DescendingOrder
            self._table.sortByColumn(int(sort_col), order)
            self._last_sort_section = int(sort_col)

            # Splitter state & sizes
            splitter_hex = state.get("splitter_state")
            if splitter_hex:
                try:
                    self._splitter.restoreState(
                        QByteArray.fromHex(splitter_hex.encode())
                    )
                except Exception:
                    pass

            # Details height & state
            details_h = state.get("details_height")
            if details_h is not None:
                try:
                    self._details_height = max(140, int(details_h))
                except (ValueError, TypeError):
                    self._details_height = 250
                details_vis = state.get("details_visible", True)
            else:
                splitter_sizes = state.get("splitter_sizes")
                if splitter_sizes and isinstance(splitter_sizes, list) and len(splitter_sizes) == 2 and int(splitter_sizes[1]) >= 50:
                    self._details_height = max(140, int(splitter_sizes[1]))
                    details_vis = state.get("details_visible", True)
                else:
                    self._details_height = 250
                    # Auto-heal vanished legacy state where collapsed to 0
                    details_vis = True

            details_state = state.get("details_state")
            if details_state and isinstance(details_state, dict):
                self._details_panel.restore_state(details_state)

            details_vis = bool(details_vis)
            self._act_toggle_details.setChecked(details_vis)
            self._details_panel.setVisible(details_vis)

            sizes = self._splitter.sizes()
            total = sum(sizes) if sum(sizes) > 250 else (self.height() or 700)
            safe_bot = min(total - 100, max(140, self._details_height))
            safe_top = max(100, total - safe_bot)

            if details_vis:
                self._splitter.setSizes([safe_top, safe_bot])
            else:
                self._splitter.setSizes([total, 0])

            tip = (
                "Hide bottom panel (F4)"
                if details_vis
                else "Show bottom panel (F4)"
            )
            self._act_toggle_details.setToolTip(tip)
            self._sync_panel_buttons()
        except Exception as exc:
            log.warning("Failed to restore window state from DB: %s", exc)

    def changeEvent(self, event: QEvent):
        if event.type() == QEvent.Type.WindowStateChange:
            cfg = self._manager.general_config
            if self.isMinimized() and cfg.enable_system_tray and cfg.minimize_to_tray:
                QTimer.singleShot(0, self.hide)
        super().changeEvent(event)
        self._update_manager_window_visibility()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_tray_menu_text()
        self._update_manager_window_visibility()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._update_tray_menu_text()
        self._update_manager_window_visibility()

    def _update_manager_window_visibility(self):
        """Update DownloadManager with current window visibility state for AnimePahe embedding."""
        if hasattr(self, "_manager") and self._manager:
            # Window is usable for embedding only when visible and not minimized
            visible = self.isVisible() and not self.isMinimized()
            self._manager.set_window_visible(visible)

    def closeEvent(self, event):
        cfg = self._manager.general_config
        if not getattr(self, "_force_exit", False) and cfg.enable_system_tray and cfg.close_to_tray:
            event.ignore()
            self.hide()
            if self._tray_icon and self._tray_icon.isVisible() and not getattr(self, "_close_to_tray_notified", False):
                self._close_to_tray_notified = True
                try:
                    self._tray_icon.showMessage(
                        "Running in Background",
                        "My-IDM is running in the system tray and continuing downloads in the background.",
                        QSystemTrayIcon.MessageIcon.Information,
                        3000,
                    )
                except Exception:
                    pass
            self._update_tray_menu_text()
            return

        if getattr(self, "_is_closing", False):
            event.accept()
            return
        self._is_closing = True

        try:
            from my_idm.notifications import unregister_notification_handler
            unregister_notification_handler(self.show_tray_notification)
        except Exception:
            pass

        # Stop UI timer immediately so no further GUI updates fire
        if hasattr(self, "_details_timer"):
            self._details_timer.stop()

        # Save UI state before hiding so geometry is accurate
        try:
            self._save_ui_state_to_db()
        except Exception as exc:
            log.warning("Failed saving UI state: %s", exc)

        exit_splash = None
        if self._show_exit_splash:
            try:
                from my_idm.splash import IDMExitSplashScreen
                exit_splash = IDMExitSplashScreen()
                # Center exit splash over the closing main window
                splash_x = self.x() + (self.width() - exit_splash.width()) // 2
                splash_y = self.y() + (self.height() - exit_splash.height()) // 2
                exit_splash.move(max(0, splash_x), max(0, splash_y))
                exit_splash.show()
                exit_splash.set_message("Closing My-IDM...", 10)
            except Exception as exc:
                log.warning("Could not show exit splash: %s", exc)
                exit_splash = None

        # Instantly hide main window
        self.hide()

        # Flush all pending window manager messages so main window vanishes immediately
        # and exit splash screen appears on screen with zero delay
        app = QApplication.instance()
        if app:
            app.processEvents()

        try:
            if exit_splash:
                self._manager.stop(status_cb=exit_splash.set_message)
                exit_splash.set_message("Goodbye!", 100)
                exit_splash.close()
                if app:
                    app.processEvents()
            else:
                self._manager.stop()
        finally:
            super().closeEvent(event)
