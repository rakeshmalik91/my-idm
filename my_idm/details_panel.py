"""Bottom details panel for inspecting downloads (Overview, Files, Peers, Trackers, Segments)."""

from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import humanize
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from my_idm.database import DownloadEntry
from my_idm.download_model import _format_eta, _format_speed, _format_time
from my_idm.manager import DownloadManager
from my_idm.styles import Colors


_TORRENT_PRIORITY_MAP = {
    7: "High",
    4: "Normal",
    1: "Low",
    0: "Don't Download",
}

_PRIORITY_TO_VAL = {
    "High": 7,
    "Normal": 4,
    "Low": 1,
    "Don't Download": 0,
}


def _to_str(val: Any) -> str:
    """Safely convert any value (including bytes from libtorrent) to a unicode string."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


class DetailsPanel(QWidget):
    """Collapsible and tabbed bottom panel showing details for the selected download."""

    close_requested = Signal()

    def __init__(self, manager: DownloadManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._manager = manager
        self._download_id: Optional[str] = None
        self._current_entry: Optional[DownloadEntry] = None

        self._setup_ui()

    @property
    def current_download_id(self) -> Optional[str]:
        return self._download_id

    # -- UI Setup -------------------------------------------------------------

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 4, 8, 8)
        main_layout.setSpacing(6)

        # Header Bar
        header_widget = QWidget(self)
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(4, 2, 4, 2)
        header_layout.setSpacing(8)

        self._lbl_icon = QLabel("📊", header_widget)
        self._lbl_icon.setStyleSheet("font-size: 16px;")
        header_layout.addWidget(self._lbl_icon)

        self._lbl_title = QLabel("Select a download to view details", header_widget)
        font = self._lbl_title.font()
        font.setBold(True)
        font.setPointSize(11)
        self._lbl_title.setFont(font)
        self._lbl_title.setStyleSheet(f"color: {Colors.TEXT};")
        header_layout.addWidget(self._lbl_title, stretch=1)

        self._lbl_badge = QLabel("", header_widget)
        self._lbl_badge.setStyleSheet(
            f"background-color: {Colors.BG_LIGHT}; color: {Colors.ACCENT}; "
            f"padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;"
        )
        self._lbl_badge.setVisible(False)
        header_layout.addWidget(self._lbl_badge)

        self._btn_open_folder = QPushButton("📁 Open Folder", header_widget)
        self._btn_open_folder.setToolTip("Open containing directory in File Explorer")
        self._btn_open_folder.clicked.connect(self._on_open_folder_clicked)
        self._btn_open_folder.setVisible(False)
        header_layout.addWidget(self._btn_open_folder)

        self._btn_close = QPushButton("✕", header_widget)
        self._btn_close.setToolTip("Hide details panel (F4)")
        self._btn_close.setFixedSize(26, 26)
        self._btn_close.clicked.connect(self.close_requested.emit)
        header_layout.addWidget(self._btn_close)

        main_layout.addWidget(header_widget)

        # Tab Widget
        self._tabs = QTabWidget(self)

        # 1. Overview Tab
        self._tab_overview = self._create_overview_tab()
        self._tabs.addTab(self._tab_overview, "📋 Overview")

        # 2. Files Tab
        self._tab_files = self._create_files_tab()
        self._tabs.addTab(self._tab_files, "📁 Files")

        # 3. Peers & Swarm Tab
        self._tab_peers = self._create_peers_tab()
        self._tabs.addTab(self._tab_peers, "👥 Peers & Swarm")

        # 4. Trackers Tab
        self._tab_trackers = self._create_trackers_tab()
        self._tabs.addTab(self._tab_trackers, "📡 Trackers")

        # 5. Segments Tab
        self._tab_segments = self._create_segments_tab()
        self._tabs.addTab(self._tab_segments, "🧩 Segments")

        main_layout.addWidget(self._tabs, stretch=1)

    def _create_overview_tab(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # Info grid container
        grid_widget = QWidget(container)
        grid_layout = QHBoxLayout(grid_widget)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(24)

        # Left column
        left_col = QVBoxLayout()
        left_col.setSpacing(6)

        self._ov_status = self._create_info_row(left_col, "Status:")
        self._ov_size = self._create_info_row(left_col, "Size:")
        self._ov_downloaded = self._create_info_row(left_col, "Downloaded:")
        self._ov_speed = self._create_info_row(left_col, "Speed:")
        self._ov_eta = self._create_info_row(left_col, "ETA:")
        self._ov_added = self._create_info_row(left_col, "Added:")
        self._ov_completed = self._create_info_row(left_col, "Completed:")
        left_col.addStretch()
        grid_layout.addLayout(left_col, stretch=1)

        # Right column
        right_col = QVBoxLayout()
        right_col.setSpacing(6)

        self._ov_type = self._create_info_row(right_col, "Transfer Type:")
        self._ov_swarm = self._create_info_row(right_col, "Swarm / Parts:")
        self._ov_save_path = self._create_info_row(right_col, "Save Directory:")
        self._ov_hash = self._create_info_row(right_col, "Content Hash / Infohash:")
        self._ov_security = self._create_info_row(right_col, "Malware Scan:")
        self._ov_url = self._create_info_row(right_col, "Source URL / Magnet:")
        right_col.addStretch()
        grid_layout.addLayout(right_col, stretch=1)

        layout.addWidget(grid_widget)
        scroll.setWidget(container)
        return scroll

    def _create_info_row(self, parent_layout: QVBoxLayout, label_text: str) -> QLabel:
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label_text)
        lbl.setFixedWidth(160)
        lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-weight: 500;")
        val = QLabel("—")
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        val.setWordWrap(True)
        row.addWidget(lbl)
        row.addWidget(val, stretch=1)
        parent_layout.addLayout(row)
        return val

    def _create_files_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._table_files = QTableWidget(0, 6, container)
        self._table_files.setHorizontalHeaderLabels([
            "#", "File Name", "Size", "Progress", "Priority", "Status"
        ])
        header = self._table_files.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table_files.setColumnWidth(3, 140)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._table_files.setColumnWidth(4, 130)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table_files.verticalHeader().setVisible(False)
        self._table_files.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_files.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self._table_files)
        return container

    def _create_peers_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._lbl_peers_status = QLabel("", container)
        self._lbl_peers_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(self._lbl_peers_status)

        self._table_peers = QTableWidget(0, 6, container)
        self._table_peers.setHorizontalHeaderLabels([
            "IP Address : Port", "Client", "Progress", "Down Speed", "Up Speed", "Flags"
        ])
        header = self._table_peers.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table_peers.verticalHeader().setVisible(False)
        self._table_peers.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_peers.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self._table_peers)
        return container

    def _create_trackers_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._lbl_trackers_status = QLabel("", container)
        self._lbl_trackers_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(self._lbl_trackers_status)

        self._table_trackers = QTableWidget(0, 4, container)
        self._table_trackers.setHorizontalHeaderLabels([
            "Tier", "Tracker URL", "Status", "Send Stats"
        ])
        header = self._table_trackers.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table_trackers.verticalHeader().setVisible(False)
        self._table_trackers.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_trackers.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self._table_trackers)
        return container

    def _create_segments_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._lbl_segments_status = QLabel("", container)
        self._lbl_segments_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(self._lbl_segments_status)

        self._table_segments = QTableWidget(0, 5, container)
        self._table_segments.setHorizontalHeaderLabels([
            "Segment #", "Byte Range", "Downloaded", "Progress", "Status"
        ])
        header = self._table_segments.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table_segments.setColumnWidth(3, 140)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table_segments.verticalHeader().setVisible(False)
        self._table_segments.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_segments.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self._table_segments)
        return container

    # -- Public control -------------------------------------------------------

    def set_download_id(self, download_id: Optional[str]):
        """Set or clear the inspected download ID and refresh views."""
        self._download_id = download_id
        self.refresh()

    def _get_entry(self, download_id: str) -> Optional[DownloadEntry]:
        win = self.window()
        if win is not None and hasattr(win, "_model") and hasattr(win._model, "get_entry_by_id"):
            entry = win._model.get_entry_by_id(download_id)
            if entry:
                return entry
        return self._manager.get_entry(download_id)

    def refresh(self):
        """Update all tabs for the active download."""
        if not self._download_id:
            self._clear_view()
            return

        entry = self._get_entry(self._download_id)
        if not entry:
            self._clear_view()
            return

        self._current_entry = entry
        self._update_header(entry)
        self._update_overview(entry)
        self._update_files(entry)
        self._update_peers(entry)
        self._update_trackers(entry)
        self._update_segments(entry)

    # -- Internal update methods ----------------------------------------------

    def _clear_view(self):
        self._lbl_icon.setText("📊")
        self._lbl_title.setText("Select a download to view details")
        self._lbl_badge.setVisible(False)
        self._btn_open_folder.setVisible(False)

        # Clear overview
        self._ov_status.setText("—")
        self._ov_size.setText("—")
        self._ov_downloaded.setText("—")
        self._ov_speed.setText("—")
        self._ov_eta.setText("—")
        self._ov_added.setText("—")
        self._ov_completed.setText("—")
        self._ov_type.setText("—")
        self._ov_swarm.setText("—")
        self._ov_save_path.setText("—")
        self._ov_hash.setText("—")
        self._ov_security.setText("—")
        self._ov_url.setText("—")

        self._table_files.setRowCount(0)
        self._table_peers.setRowCount(0)
        self._table_trackers.setRowCount(0)
        self._table_segments.setRowCount(0)

        self._lbl_peers_status.setText("")
        self._lbl_trackers_status.setText("")
        self._lbl_segments_status.setText("")

    def _update_header(self, entry: DownloadEntry):
        icon = "📦" if entry.download_type == "torrent" else "🌐"
        self._lbl_icon.setText(icon)

        name = entry.filename or os.path.basename(entry.file_path) if entry.file_path else "Download"
        self._lbl_title.setText(name)

        badge_type = "BitTorrent" if entry.download_type == "torrent" else "HTTP / Direct"
        self._lbl_badge.setText(badge_type)
        self._lbl_badge.setVisible(True)
        self._btn_open_folder.setVisible(bool(entry.save_path or entry.file_path))

    def _update_overview(self, entry: DownloadEntry):
        # Status with color
        status_color = Colors.ACCENT
        if entry.status == "completed":
            status_color = Colors.GREEN
        elif entry.status == "seeding":
            status_color = Colors.PURPLE
        elif entry.status == "error":
            status_color = Colors.RED
        elif entry.status == "paused":
            status_color = Colors.ORANGE
        elif entry.status == "threat_detected":
            status_color = Colors.RED

        err = f" ({entry.error_message})" if entry.error_message else ""
        self._ov_status.setText(f"<span style='color: {status_color}; font-weight: bold;'>{entry.status.capitalize()}</span>{err}")

        # Size & progress
        total_str = humanize.naturalsize(entry.total_size, binary=True) if entry.total_size > 0 else "Unknown"
        dl_str = humanize.naturalsize(entry.downloaded_size, binary=True)
        pct = (entry.downloaded_size / entry.total_size * 100) if entry.total_size > 0 else 0.0
        self._ov_size.setText(f"{total_str} ({pct:.1f}%)")
        self._ov_downloaded.setText(f"{dl_str} / {total_str}")

        # Speeds
        down_speed = _format_speed(entry.speed)
        up_speed = _format_speed(entry.upload_speed)
        if entry.download_type == "torrent":
            self._ov_speed.setText(f"↓ {down_speed}   |   ↑ {up_speed}")
            self._ov_swarm.setText(f"{entry.seeds} seeds, {entry.peers} peers connected")
        else:
            self._ov_speed.setText(f"↓ {down_speed}")
            self._ov_swarm.setText(f"{entry.num_segments} HTTP parallel segments")

        self._ov_eta.setText(_format_eta(entry.eta_seconds))
        self._ov_added.setText(_format_time(entry.added_at))
        self._ov_completed.setText(_format_time(entry.completed_at))

        self._ov_type.setText("BitTorrent Swarm" if entry.download_type == "torrent" else "HTTP / Multi-Segment")
        self._ov_save_path.setText(entry.save_path or "—")

        # Hash / Infohash
        hash_val = entry.torrent_info_hash or entry.content_hash or entry.etag or "—"
        self._ov_hash.setText(hash_val)

        # Security
        meta = entry.metadata
        if meta.get("threat_detected"):
            rep = meta.get("antivirus_report", "Malware detected!")
            self._ov_security.setText(f"<span style='color: {Colors.RED}; font-weight: bold;'>⚠ Threat Detected: {html.escape(rep)}</span>")
        elif meta.get("antivirus_scanned"):
            self._ov_security.setText(f"<span style='color: {Colors.GREEN}; font-weight: bold;'>✔ Clean (Scanned)</span>")
        else:
            self._ov_security.setText("<span style='color: {Colors.TEXT_SECONDARY};'>Not scanned yet</span>")

        # URL
        url_text = entry.url
        if len(url_text) > 80:
            url_text = url_text[:77] + "..."
        self._ov_url.setText(url_text)

    def _update_files(self, entry: DownloadEntry):
        files = self._manager.get_download_files(entry.id)
        if not files:
            self._table_files.setRowCount(0)
            return

        rebuild = self._table_files.rowCount() != len(files)
        if rebuild:
            self._table_files.setRowCount(len(files))

        for row, f in enumerate(files):
            # Index
            idx_item = self._table_files.item(row, 0)
            if not idx_item:
                idx_item = QTableWidgetItem()
                idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_files.setItem(row, 0, idx_item)
            idx_item.setText(_to_str(f.get("index", row + 1)))

            # Path / Name
            name_item = self._table_files.item(row, 1)
            if not name_item:
                name_item = QTableWidgetItem()
                self._table_files.setItem(row, 1, name_item)
            name_item.setText(_to_str(f.get("path", "file")))

            # Size
            size_val = f.get("size", 0)
            size_str = humanize.naturalsize(size_val, binary=True) if size_val > 0 else "—"
            size_item = self._table_files.item(row, 2)
            if not size_item:
                size_item = QTableWidgetItem()
                size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table_files.setItem(row, 2, size_item)
            size_item.setText(size_str)

            # Progress bar
            pct_float = f.get("progress", 0.0)
            prog_bar = self._table_files.cellWidget(row, 3)
            if not isinstance(prog_bar, QProgressBar):
                prog_bar = QProgressBar()
                prog_bar.setRange(0, 100)
                prog_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
                prog_bar.setStyleSheet(
                    f"QProgressBar {{ border: 1px solid {Colors.BORDER}; border-radius: 3px; background: {Colors.BG_DARK}; height: 16px; text-align: center; font-size: 11px; }} "
                    f"QProgressBar::chunk {{ background: {Colors.ACCENT}; border-radius: 2px; }}"
                )
                self._table_files.setCellWidget(row, 3, prog_bar)
            prog_bar.setValue(int(pct_float * 100))

            # Priority combo (Torrents only)
            if entry.download_type == "torrent":
                combo = self._table_files.cellWidget(row, 4)
                if not isinstance(combo, QComboBox):
                    combo = QComboBox()
                    for p_text in ["High", "Normal", "Low", "Don't Download"]:
                        combo.addItem(p_text, _PRIORITY_TO_VAL[p_text])
                    # Connect change signal
                    file_idx = f.get("index", row)
                    combo.currentIndexChanged.connect(
                        lambda idx, c=combo, f_idx=file_idx: self._on_file_priority_changed(f_idx, c)
                    )
                    self._table_files.setCellWidget(row, 4, combo)

                curr_prio = f.get("priority", 4)
                combo.blockSignals(True)
                prio_name = _TORRENT_PRIORITY_MAP.get(curr_prio, "Normal")
                combo.setCurrentText(prio_name)
                combo.blockSignals(False)
            else:
                prio_item = self._table_files.item(row, 4)
                if not prio_item:
                    prio_item = QTableWidgetItem("Normal")
                    prio_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self._table_files.setItem(row, 4, prio_item)

            # Status
            status_str = f.get("status", "pending")
            status_item = self._table_files.item(row, 5)
            if not status_item:
                status_item = QTableWidgetItem()
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_files.setItem(row, 5, status_item)
            status_item.setText(_to_str(status_str).capitalize())

    def _on_file_priority_changed(self, file_index: int, combo: QComboBox):
        if not self._download_id:
            return
        prio_val = combo.currentData()
        if prio_val is not None:
            self._manager.set_torrent_file_priority(self._download_id, file_index, prio_val)

    def _update_peers(self, entry: DownloadEntry):
        if entry.download_type != "torrent":
            self._lbl_peers_status.setText("Peer and swarm monitoring is only active for BitTorrent transfers.")
            self._table_peers.setRowCount(0)
            return

        peers = self._manager.get_torrent_peers(entry.id)
        self._lbl_peers_status.setText(f"{len(peers)} connected peer(s) in active swarm")

        rebuild = self._table_peers.rowCount() != len(peers)
        if rebuild:
            self._table_peers.setRowCount(len(peers))

        for row, p in enumerate(peers):
            # IP : Port
            ip_item = self._table_peers.item(row, 0)
            if not ip_item:
                ip_item = QTableWidgetItem()
                self._table_peers.setItem(row, 0, ip_item)
            ip_item.setText(_to_str(p.get("ip", "—")))

            # Client
            client_item = self._table_peers.item(row, 1)
            if not client_item:
                client_item = QTableWidgetItem()
                self._table_peers.setItem(row, 1, client_item)
            client_item.setText(_to_str(p.get("client", "Unknown")))

            # Progress
            prog_item = self._table_peers.item(row, 2)
            if not prog_item:
                prog_item = QTableWidgetItem()
                prog_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_peers.setItem(row, 2, prog_item)
            prog_val = p.get('progress', 0.0)
            # Handle either 0.0-1.0 or 0-100 float
            if prog_val > 1.0:
                prog_pct = prog_val
            else:
                prog_pct = prog_val * 100.0
            prog_item.setText(f"{prog_pct:.1f}%")

            # Down Speed
            down_item = self._table_peers.item(row, 3)
            if not down_item:
                down_item = QTableWidgetItem()
                down_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table_peers.setItem(row, 3, down_item)
            down_item.setText(_format_speed(p.get("down_speed", 0.0)))

            # Up Speed
            up_item = self._table_peers.item(row, 4)
            if not up_item:
                up_item = QTableWidgetItem()
                up_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table_peers.setItem(row, 4, up_item)
            up_item.setText(_format_speed(p.get("up_speed", 0.0)))

            # Flags
            flags_item = self._table_peers.item(row, 5)
            if not flags_item:
                flags_item = QTableWidgetItem()
                self._table_peers.setItem(row, 5, flags_item)
            flags_item.setText(_to_str(p.get("flags", "")))

    def _update_trackers(self, entry: DownloadEntry):
        if entry.download_type != "torrent":
            self._lbl_trackers_status.setText("Trackers are only used for BitTorrent downloads.")
            self._table_trackers.setRowCount(0)
            return

        trackers = self._manager.get_torrent_trackers(entry.id)
        self._lbl_trackers_status.setText(f"{len(trackers)} tracker(s) announced")

        rebuild = self._table_trackers.rowCount() != len(trackers)
        if rebuild:
            self._table_trackers.setRowCount(len(trackers))

        for row, t in enumerate(trackers):
            # Tier
            tier_item = self._table_trackers.item(row, 0)
            if not tier_item:
                tier_item = QTableWidgetItem()
                tier_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_trackers.setItem(row, 0, tier_item)
            tier_item.setText(_to_str(t.get("tier", 0)))

            # URL
            url_item = self._table_trackers.item(row, 1)
            if not url_item:
                url_item = QTableWidgetItem()
                self._table_trackers.setItem(row, 1, url_item)
            url_item.setText(_to_str(t.get("url", "")))

            # Status
            status_item = self._table_trackers.item(row, 2)
            if not status_item:
                status_item = QTableWidgetItem()
                self._table_trackers.setItem(row, 2, status_item)
            status_item.setText(_to_str(t.get("status", "Working")))

            # Send Stats
            stats_item = self._table_trackers.item(row, 3)
            if not stats_item:
                stats_item = QTableWidgetItem()
                stats_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_trackers.setItem(row, 3, stats_item)
            stats_item.setText("Yes" if t.get("send_stats") else "No")

    def _update_segments(self, entry: DownloadEntry):
        if entry.download_type == "torrent":
            self._lbl_segments_status.setText("BitTorrent divides transfers across peer pieces rather than fixed byte ranges.")
            self._table_segments.setRowCount(0)
            return

        segments = self._manager.get_download_segments(entry.id)
        self._lbl_segments_status.setText(f"{len(segments)} segment(s) configured for parallel download")

        rebuild = self._table_segments.rowCount() != len(segments)
        if rebuild:
            self._table_segments.setRowCount(len(segments))

        for row, s in enumerate(segments):
            # Index
            idx_item = self._table_segments.item(row, 0)
            if not idx_item:
                idx_item = QTableWidgetItem()
                idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_segments.setItem(row, 0, idx_item)
            idx_item.setText(f"Segment #{s.index + 1}")

            # Byte Range
            range_str = f"{s.start_byte:,} – {s.end_byte:,}" if s.end_byte > 0 else f"{s.start_byte:,} – end"
            range_item = self._table_segments.item(row, 1)
            if not range_item:
                range_item = QTableWidgetItem()
                self._table_segments.setItem(row, 1, range_item)
            range_item.setText(range_str)

            # Downloaded
            dl_str = humanize.naturalsize(s.downloaded_bytes, binary=True)
            dl_item = self._table_segments.item(row, 2)
            if not dl_item:
                dl_item = QTableWidgetItem()
                dl_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table_segments.setItem(row, 2, dl_item)
            dl_item.setText(dl_str)

            # Progress bar
            seg_len = (s.end_byte - s.start_byte + 1) if s.end_byte >= s.start_byte else 0
            pct = int((s.downloaded_bytes / seg_len * 100)) if seg_len > 0 else 0
            pct = min(100, max(0, pct))

            prog_bar = self._table_segments.cellWidget(row, 3)
            if not isinstance(prog_bar, QProgressBar):
                prog_bar = QProgressBar()
                prog_bar.setRange(0, 100)
                prog_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
                prog_bar.setStyleSheet(
                    f"QProgressBar {{ border: 1px solid {Colors.BORDER}; border-radius: 3px; background: {Colors.BG_DARK}; height: 16px; text-align: center; font-size: 11px; }} "
                    f"QProgressBar::chunk {{ background: {Colors.GREEN}; border-radius: 2px; }}"
                )
                self._table_segments.setCellWidget(row, 3, prog_bar)
            prog_bar.setValue(pct)

            # Status
            status_item = self._table_segments.item(row, 4)
            if not status_item:
                status_item = QTableWidgetItem()
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_segments.setItem(row, 4, status_item)
            status_item.setText(_to_str(s.status).capitalize())

    # -- Actions --------------------------------------------------------------

    def _on_open_folder_clicked(self):
        if not self._current_entry:
            return
        folder = self._current_entry.save_path
        file_path = self._current_entry.file_path
        if file_path and Path(file_path).exists():
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", file_path.replace("/", "\\")])
            else:
                os.startfile(folder)
        elif folder and Path(folder).exists():
            os.startfile(folder)
