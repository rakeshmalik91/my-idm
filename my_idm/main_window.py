"""Main application window for My-IDM."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QSettings, QPointF, QTimer, QByteArray
from PySide6.QtGui import (
    QAction,
    QColor,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableView,
    QToolBar,
    QToolButton,
    QWidget,
)

from my_idm.database import Database, DownloadEntry
from my_idm.delegates import ProgressBarDelegate
from my_idm.details_panel import DetailsPanel
from my_idm.dialogs import AddDownloadDialog, DeleteConfirmDialog, MoveDownloadDialog
from my_idm.download_model import Col, DownloadTableModel
from my_idm.manager import DownloadManager
from my_idm.resources import get_app_icon, get_app_logo_pixmap
from my_idm.network import NetworkConfig, is_vpn_adapter_name
from my_idm.network_dialog import NetworkSettingsDialog
from my_idm.security import SecurityConfig
from my_idm.config import TorConfig
from my_idm.security_dialog import SecuritySettingsDialog
from my_idm.settings_dialog import SettingsDialog
from my_idm.styles import Colors

log = logging.getLogger(__name__)


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


class MainWindow(QMainWindow):
    """The main My-IDM window."""

    def __init__(self, manager: DownloadManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._manager = manager

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
        self._table.horizontalHeader().sectionClicked.connect(
            self._on_header_section_clicked
        )
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)

        # Progress bar delegate
        self._progress_delegate = ProgressBarDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.PROGRESS, self._progress_delegate
        )

        # Column sizing - make all columns interactively resizable
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setCascadingSectionResizes(False)
        header.setDefaultSectionSize(110)

        self._model.set_tor_config(self._manager.tor_config)

        # Set specific default column widths
        self._table.setColumnWidth(Col.NAME, 240)
        self._table.setColumnWidth(Col.SIZE, 90)
        self._table.setColumnWidth(Col.PROGRESS, 160)
        self._table.setColumnWidth(Col.STATUS, 135)
        self._table.setColumnWidth(Col.SPEED, 110)
        self._table.setColumnWidth(Col.ETA, 80)
        self._table.setColumnWidth(Col.TYPE, 80)
        self._table.setColumnWidth(Col.SEEDS_PEERS, 100)
        self._table.setColumnWidth(Col.ADDED, 130)
        self._table.setColumnWidth(Col.LAST_TRIED, 130)
        self._table.setColumnWidth(Col.COMPLETED, 130)
        self._table.setColumnWidth(Col.SAVE_PATH, 220)

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
        self._splitter.addWidget(self._details_panel)
        self._splitter.setSizes([450, 250])

        self.setCentralWidget(self._splitter)

    def _setup_actions(self):
        """Create all QActions."""
        self._act_add = QAction("➕ Add Download", self)
        self._act_add.setShortcut(QKeySequence("Ctrl+N"))
        self._act_add.setToolTip("Add URL, Magnet Link, or .torrent file (Ctrl+N)")
        self._act_add.triggered.connect(self._on_add)

        self._act_add_torrent = QAction("📦 Add Torrent File…", self)
        self._act_add_torrent.setShortcut(QKeySequence("Ctrl+T"))
        self._act_add_torrent.setToolTip("Add .torrent file (Ctrl+T)")
        self._act_add_torrent.triggered.connect(self._on_add_torrent)

        self._act_resume = QAction(_create_play_icon(), "Resume", self)
        self._act_resume.setShortcut(QKeySequence("Ctrl+R"))
        self._act_resume.setToolTip("Resume selected downloads (Ctrl+R)")
        self._act_resume.triggered.connect(self._on_resume)

        self._act_pause = QAction(_create_pause_icon(), "Pause", self)
        self._act_pause.setShortcut(QKeySequence("Space"))
        self._act_pause.setToolTip("Pause selected downloads (Space)")
        self._act_pause.triggered.connect(self._on_pause)

        self._act_copy_url = QAction("📋 Copy URL / Magnet", self)
        self._act_copy_url.setShortcut(QKeySequence("Ctrl+C"))
        self._act_copy_url.setToolTip("Copy download URL or Magnet link to clipboard (Ctrl+C)")
        self._act_copy_url.triggered.connect(self._on_copy_url)

        self._act_delete = QAction("🗑 Delete", self)
        self._act_delete.setShortcut(QKeySequence("Delete"))
        self._act_delete.setToolTip("Delete selected downloads")
        self._act_delete.triggered.connect(self._on_delete)

        self._act_move = QAction("📂 Move", self)
        self._act_move.setToolTip("Move download to another directory")
        self._act_move.triggered.connect(self._on_move)

        self._act_recheck = QAction("🔄 Recheck", self)
        self._act_recheck.setToolTip("Verify existing files on disk")
        self._act_recheck.triggered.connect(self._on_recheck)

        self._act_open_file = QAction("📄 Open File", self)
        self._act_open_file.setShortcut(QKeySequence("Return"))
        self._act_open_file.setToolTip("Open the downloaded file")
        self._act_open_file.triggered.connect(self._on_open_file)

        self._act_open_folder = QAction("📁 Open Folder", self)
        self._act_open_folder.setShortcut(QKeySequence("Ctrl+O"))
        self._act_open_folder.setToolTip("Open containing folder (Ctrl+O)")
        self._act_open_folder.triggered.connect(self._on_open_folder)

        self._act_load_backlog = QAction("📋 Load Backlog", self)
        self._act_load_backlog.setShortcut(QKeySequence("Ctrl+L"))
        self._act_load_backlog.setToolTip("Load URLs from a backlog file")
        self._act_load_backlog.triggered.connect(self._on_load_backlog)

        self._act_preferences = QAction("⚙️ Preferences…", self)
        self._act_preferences.setShortcut(QKeySequence("Ctrl+,"))
        self._act_preferences.setToolTip(
            "Configure default download folder, performance, network, and security (Ctrl+,)"
        )
        self._act_preferences.triggered.connect(
            lambda: self._on_open_preferences(0)
        )

        self._act_network_settings = QAction("🌐 VPN & Network Settings…", self)
        self._act_network_settings.setToolTip(
            "Configure VPN adapter binding, Kill Switch, and Proxy"
        )
        self._act_network_settings.triggered.connect(
            self._on_open_network_settings
        )

        self._act_security_settings = QAction("🛡️ Antivirus & Security Settings…", self)
        self._act_security_settings.setToolTip(
            "Configure pre-download URL inspection and post-download antivirus scanning"
        )
        self._act_security_settings.triggered.connect(
            self._on_open_security_settings
        )

        self._act_scan_antivirus = QAction("🛡️ Scan with Antivirus", self)
        self._act_scan_antivirus.setToolTip("Scan the downloaded file with antivirus")
        self._act_scan_antivirus.triggered.connect(self._on_scan_selected_file)

        self._act_toggle_details = QAction("📋 Details Panel", self)
        self._act_toggle_details.setCheckable(True)
        self._act_toggle_details.setChecked(True)
        self._act_toggle_details.setShortcut(QKeySequence("F4"))
        self._act_toggle_details.setToolTip("Toggle bottom download details panel (F4)")
        self._act_toggle_details.toggled.connect(self._on_toggle_details)
        self._details_panel.close_requested.connect(
            lambda: self._act_toggle_details.setChecked(False)
        )

        self._act_tor = QAction("🧅 Tor: OFF", self)
        self._act_tor.setCheckable(True)
        self._act_tor.setToolTip("Toggle Tor network privacy routing (SOCKS5 proxy)")
        self._act_tor.toggled.connect(self._on_toggle_tor)

    def _setup_toolbar(self):
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        toolbar.addAction(self._act_add)
        toolbar.addSeparator()
        toolbar.addAction(self._act_resume)
        toolbar.addAction(self._act_pause)
        toolbar.addSeparator()
        toolbar.addAction(self._act_delete)
        toolbar.addAction(self._act_move)
        toolbar.addAction(self._act_recheck)
        toolbar.addSeparator()
        toolbar.addAction(self._act_open_file)
        toolbar.addAction(self._act_open_folder)
        toolbar.addSeparator()
        toolbar.addAction(self._act_tor)
        toolbar.addSeparator()
        toolbar.addAction(self._act_toggle_details)
        toolbar.addAction(self._act_preferences)

        # Show only icons without text for play and pause buttons on the toolbar
        for act in (self._act_resume, self._act_pause):
            btn = toolbar.widgetForAction(act)
            if isinstance(btn, QToolButton):
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        self.addToolBar(toolbar)

    def _setup_menubar(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(self._act_add)
        file_menu.addAction(self._act_add_torrent)
        file_menu.addAction(self._act_load_backlog)
        file_menu.addSeparator()

        exit_act = QAction("Exit", self)
        exit_act.setShortcut(QKeySequence("Ctrl+Q"))
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        # Edit menu
        edit_menu = menubar.addMenu("&Edit")
        edit_menu.addAction(self._act_resume)
        edit_menu.addAction(self._act_pause)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_copy_url)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_delete)
        edit_menu.addAction(self._act_move)
        edit_menu.addAction(self._act_recheck)

        # View menu
        view_menu = menubar.addMenu("&View")
        view_menu.addAction(self._act_toggle_details)
        view_menu.addSeparator()
        select_all_act = QAction("Select All", self)
        select_all_act.setShortcut(QKeySequence("Ctrl+A"))
        select_all_act.triggered.connect(self._table.selectAll)
        view_menu.addAction(select_all_act)

        view_menu.addSeparator()
        sort_menu = view_menu.addMenu("&Sort By")
        sort_columns = [
            ("Date Added (Default)", Col.ADDED),
            ("Name", Col.NAME),
            ("Size", Col.SIZE),
            ("Progress", Col.PROGRESS),
            ("Status", Col.STATUS),
            ("Speed", Col.SPEED),
            ("ETA", Col.ETA),
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
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_tor)
        self._act_tor_settings = QAction("🧅 Tor Network Settings...", self)
        self._act_tor_settings.triggered.connect(self._on_open_tor_settings)
        tools_menu.addAction(self._act_tor_settings)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_network_settings)
        tools_menu.addAction(self._act_security_settings)

        # Help menu
        help_menu = menubar.addMenu("&Help")
        about_act = QAction("About My-IDM", self)
        about_act.triggered.connect(self._on_about)
        help_menu.addAction(about_act)

    def _setup_statusbar(self):
        self._status_label = QLabel("Ready")
        self._speed_label = QLabel("")
        self._count_label = QLabel("0 downloads")

        self._tor_status_btn = QPushButton("🧅 Tor: OFF")
        self._tor_status_btn.setFlat(True)
        self._tor_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tor_status_btn.setToolTip("Click to configure Tor Network Settings")
        self._tor_status_btn.clicked.connect(self._on_open_tor_settings)

        self._vpn_status_btn = QPushButton("🌐 Net: Default")
        self._vpn_status_btn.setFlat(True)
        self._vpn_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vpn_status_btn.setToolTip(
            "Click to configure VPN & Network Settings"
        )
        self._vpn_status_btn.clicked.connect(self._on_open_network_settings)

        status_bar = QStatusBar()
        status_bar.addWidget(self._status_label, 1)
        status_bar.addPermanentWidget(self._tor_status_btn)
        status_bar.addPermanentWidget(self._vpn_status_btn)
        status_bar.addPermanentWidget(self._speed_label)
        status_bar.addPermanentWidget(self._count_label)
        self.setStatusBar(status_bar)

        self._update_network_status_badge(self._manager.network_config)
        self._on_tor_config_changed(self._manager.tor_config)
        self._update_speed_label()

    def _connect_signals(self):
        self._manager.progress_updated.connect(self._on_progress_updated)
        self._manager.status_changed.connect(self._on_status_changed)
        self._manager.filename_resolved.connect(self._on_filename_resolved)
        self._manager.download_added.connect(self._on_download_added)
        self._manager.download_removed.connect(self._on_download_removed)
        self._manager.download_moved.connect(self._on_download_moved)
        self._manager.network_config_changed.connect(
            self._update_network_status_badge
        )
        self._manager.tor_config_changed.connect(self._on_tor_config_changed)
        self._manager.threat_detected.connect(self._on_threat_detected)

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

    # -- sorting helpers -----------------------------------------------------

    def _on_header_section_clicked(self, logical_index: int):
        if logical_index == Col.ADDED:
            # If switching to Date Added from another column, ensure it defaults to Descending (newest first)
            if self._last_sort_section != Col.ADDED:
                self._table.sortByColumn(Col.ADDED, Qt.SortOrder.DescendingOrder)
        self._last_sort_section = logical_index

    def _sort_by_column(self, col: int):
        if col == Col.ADDED:
            curr_sec = self._table.horizontalHeader().sortIndicatorSection()
            curr_ord = self._table.horizontalHeader().sortIndicatorOrder()
            if curr_sec == Col.ADDED:
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
        dlg = AddDownloadDialog(self)
        if dlg.exec() == AddDownloadDialog.DialogCode.Accepted:
            self._manager.add_download(
                dlg.url, dlg.save_path, dlg.num_segments
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

    def _on_resume(self):
        for did in self._selected_ids():
            self._manager.resume_download(did)

    def _on_delete(self):
        ids = self._selected_ids()
        if not ids:
            return
        dlg = DeleteConfirmDialog(len(ids), self)
        if dlg.exec() == DeleteConfirmDialog.DialogCode.Accepted:
            for did in ids:
                self._manager.delete_download(did, dlg.delete_files)

    def _on_move(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        dlg = MoveDownloadDialog(entry.save_path, self)
        if dlg.exec() == MoveDownloadDialog.DialogCode.Accepted:
            for did in self._selected_ids():
                self._manager.move_download(did, dlg.new_path)

    def _on_recheck(self):
        for did in self._selected_ids():
            self._manager.recheck_download(did)

    def _on_open_file(self):
        entry = self._first_selected_entry()
        if entry and entry.file_path and Path(entry.file_path).exists():
            os.startfile(entry.file_path)

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
        )
        dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
        dlg.exec()

    def _on_copy_url(self):
        entry = self._first_selected_entry()
        if entry and entry.url:
            clipboard = QGuiApplication.clipboard()
            if clipboard:
                clipboard.setText(entry.url)
                kind = "Magnet link" if entry.url.startswith("magnet:") else "URL"
                self._status_label.setText(f"Copied {kind} to clipboard")

    # -- context menu --------------------------------------------------------

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction(self._act_resume)
        menu.addAction(self._act_pause)
        menu.addSeparator()
        menu.addAction(self._act_copy_url)
        menu.addSeparator()
        menu.addAction(self._act_scan_antivirus)
        menu.addAction(self._act_recheck)
        menu.addAction(self._act_move)
        menu.addSeparator()
        menu.addAction(self._act_open_file)
        menu.addAction(self._act_open_folder)
        menu.addSeparator()
        menu.addAction(self._act_delete)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    # -- signal handlers from manager ----------------------------------------

    def _on_progress_updated(self, download_id: str, downloaded: int,
                             total: int, speed: float, eta: float,
                             seeds: int, peers: int, upload_speed: float):
        self._model.update_progress(
            download_id, downloaded, total, speed, eta,
            seeds, peers, upload_speed,
        )
        self._update_speed_label()

    def _on_status_changed(self, download_id: str, status: str,
                           error_msg: str):
        self._model.update_status(download_id, status, error_msg)
        self._update_count_label()
        self._update_speed_label()
        if self._details_panel.isVisible() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

    def _on_filename_resolved(self, download_id: str, filename: str):
        self._model.update_filename(download_id, filename)
        if self._details_panel.isVisible() and self._details_panel.current_download_id == download_id:
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

    # -- helpers -------------------------------------------------------------

    def _update_count_label(self):
        total = self._model.rowCount()
        self._count_label.setText(f"{total} download(s)")

    def _update_speed_label(self):
        down, up = self._model.get_aggregate_speeds()
        from my_idm.download_model import _format_speed
        self._speed_label.setText(f"↓ {_format_speed(down)}  ↑ {_format_speed(up)}")

    def _on_open_preferences(self, initial_tab: int = 0):
        dlg = SettingsDialog(
            general_config=self._manager.general_config,
            network_config=self._manager.network_config,
            security_config=self._manager.security_config,
            tor_config=self._manager.tor_config,
            db=self._manager._db,
            parent=self,
            initial_tab=initial_tab,
        )
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._manager.set_general_config(dlg.general_config)
            self._manager.set_network_config(dlg.network_config)
            self._manager.set_security_config(dlg.security_config)
            old_tor_enabled = self._manager.tor_config.enabled
            self._manager.set_tor_config(dlg.tor_config)
            if dlg.tor_config.enabled != old_tor_enabled:
                self._on_toggle_tor(dlg.tor_config.enabled)

    def _on_open_network_settings(self):
        self._on_open_preferences(1)

    def _on_open_tor_settings(self):
        self._on_open_preferences(2)

    def _on_open_security_settings(self):
        self._on_open_preferences(3)

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
        success, msg = self._manager.toggle_tor(checked)
        self._status_label.setText(msg)
        self._model.set_tor_config(self._manager.tor_config)
        self._table.viewport().update()
        if not success and checked:
            self._act_tor.blockSignals(True)
            self._act_tor.setChecked(False)
            self._act_tor.blockSignals(False)

            QMessageBox.critical(
                self,
                "⚠️ Tor Connection Error",
                f"Unable to activate Tor network privacy:\n\n{msg}\n\n"
                "Please verify that Tor or Tor Browser is installed, or configure the path in Tools → Tor Network Settings.",
            )

    def _on_tor_config_changed(self, config: TorConfig):
        self._model.set_tor_config(config)
        self._table.viewport().update()

        self._act_tor.blockSignals(True)
        self._act_tor.setChecked(config.enabled)
        self._act_tor.blockSignals(False)

        if config.enabled:
            self._act_tor.setText("🧅 Tor: ON")
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
            self._tor_status_btn.setStyleSheet("""
                QPushButton {
                    background: #2a1b3d;
                    color: #bd93f9;
                    border: 1px solid #bd93f9;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: #3c2457;
                }
            """)
        else:
            self._act_tor.setText("🧅 Tor: OFF")
            self._act_tor.setToolTip(
                "Tor is OFF\nClick to enable Tor routing"
            )
            self._tor_status_btn.setText("🧅 Tor: OFF")
            self._tor_status_btn.setToolTip(
                "Tor is disabled.\nClick to open Tor Settings"
            )
            self._tor_status_btn.setStyleSheet("""
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
            """)

    # -- details panel handlers ----------------------------------------------

    def _on_toggle_details(self, checked: bool):
        self._details_panel.setVisible(checked)
        if checked:
            entry = self._first_selected_entry()
            self._details_panel.set_download_id(entry.id if entry else None)

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
            splitter_sizes = self._splitter.sizes()
            details_vis = self._details_panel.isVisible()

            sort_sec = self._table.horizontalHeader().sortIndicatorSection()
            try:
                sort_ord = int(self._table.horizontalHeader().sortIndicatorOrder().value)
            except (AttributeError, TypeError, ValueError):
                sort_ord = int(Qt.SortOrder.DescendingOrder.value)

            if sort_sec < 0:
                sort_sec = Col.ADDED
                sort_ord = int(Qt.SortOrder.DescendingOrder.value)

            state = {
                "x": geom.x(),
                "y": geom.y(),
                "width": geom.width(),
                "height": geom.height(),
                "is_maximized": is_max,
                "column_widths": col_widths,
                "header_state": header_hex,
                "splitter_state": splitter_hex,
                "splitter_sizes": splitter_sizes,
                "details_visible": details_vis,
                "sort_column": sort_sec,
                "sort_order": sort_ord,
            }
            self._manager.save_ui_state(state)

            # Also sync to QSettings for backwards compatibility
            settings = QSettings("MyIDM", "My-IDM")
            settings.setValue("header_state", self._table.horizontalHeader().saveState())
            settings.setValue("splitter_state", self._splitter.saveState())
            settings.setValue("details_visible", details_vis)
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
                details_vis = settings.value("details_visible")
                if details_vis is not None:
                    self._act_toggle_details.setChecked(bool(details_vis))
                    self._details_panel.setVisible(bool(details_vis))
                self._table.horizontalHeader().setStretchLastSection(False)
                self._table.horizontalHeader().setCascadingSectionResizes(False)
                self._table.sortByColumn(Col.ADDED, Qt.SortOrder.DescendingOrder)
                self._last_sort_section = Col.ADDED
                return

            # Window size & location
            w = state.get("width")
            h = state.get("height")
            if w and h and w >= 400 and h >= 300:
                self.resize(int(w), int(h))

            x = state.get("x")
            y = state.get("y")
            if x is not None and y is not None:
                self.move(int(x), int(y))

            # Maximized or not
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

            # Ensure cascading resizes and stretch last section are not overwritten by saved state
            self._table.horizontalHeader().setStretchLastSection(False)
            self._table.horizontalHeader().setCascadingSectionResizes(False)

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

            splitter_sizes = state.get("splitter_sizes")
            if splitter_sizes and isinstance(splitter_sizes, list) and len(splitter_sizes) == 2:
                self._splitter.setSizes([int(s) for s in splitter_sizes])

            # Details panel visibility
            if "details_visible" in state:
                vis = bool(state["details_visible"])
                self._act_toggle_details.setChecked(vis)
                self._details_panel.setVisible(vis)
        except Exception as exc:
            log.warning("Failed to restore window state from DB: %s", exc)

    def closeEvent(self, event):
        self._save_ui_state_to_db()
        self._manager.stop()
        super().closeEvent(event)
