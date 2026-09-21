"""Bottom details panel for inspecting downloads (Overview, Files, Peers, Trackers, Segments)."""

from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import humanize
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from my_idm.database import DownloadEntry
from my_idm.download_model import _format_eta, _format_speed, _format_time
from my_idm.manager import DownloadManager
from my_idm.styles import Colors
from my_idm.utils import to_int


_TORRENT_PRIORITY_MAP = {
    7: "Max (100%)",
    6: "High (75%)",
    4: "Medium (50%)",
    1: "Low (25%)",
    0: "Don't Download",
}

_PRIORITY_TO_VAL = {
    "Max (100%)": 7,
    "High (75%)": 6,
    "Medium (50%)": 4,
    "Low (25%)": 1,
    "Don't Download": 0,
    # Compatibility aliases
    "Max": 7,
    "High": 6,
    "Normal": 4,
    "Medium": 4,
    "Low": 1,
}


def _priority_to_label(prio: int) -> str:
    if prio >= 7:
        return "Max (100%)"
    if prio >= 6:
        return "High (75%)"
    if prio >= 3:
        return "Medium (50%)"
    if prio >= 1:
        return "Low (25%)"
    return "Don't Download"


def _to_str(val: Any) -> str:
    """Safely convert any value (including bytes from libtorrent) to a unicode string."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


class FilesTreeWidget(QTreeWidget):
    """QTreeWidget displaying files and folders hierarchy with table compatibility methods."""

    def rowCount(self) -> int:
        return self.topLevelItemCount()

    def setRowCount(self, count: int):
        if count == 0:
            self.clear()

    def item(self, row: int, col: int):
        if 0 <= row < self.topLevelItemCount():
            top = self.topLevelItem(row)

            class _ItemCompat:
                def __init__(self, it: QTreeWidgetItem, col_idx: int):
                    self._it = it
                    self._col_idx = col_idx

                def text(self) -> str:
                    target_col = 0 if self._col_idx in (0, 2) else self._col_idx
                    t = self._it.text(target_col)
                    for p in ("📁 ", "📄 "):
                        if t.startswith(p):
                            return t[len(p):]
                    return t

            return _ItemCompat(top, col)
        return None

    def cellWidget(self, row: int, col: int):
        if 0 <= row < self.topLevelItemCount():
            top = self.topLevelItem(row)
            if col == 5:
                return self.itemWidget(top, 3)
            if col == 0:
                class _CheckCompat(QCheckBox):
                    def __init__(self, it: QTreeWidgetItem):
                        super().__init__()
                        self._it = it

                    def isChecked(self) -> bool:
                        return self._it.checkState(0) == Qt.CheckState.Checked

                    def setChecked(self, val: bool):
                        self._it.setCheckState(0, Qt.CheckState.Checked if val else Qt.CheckState.Unchecked)

                    def isEnabled(self) -> bool:
                        return bool(self._it.flags() & Qt.ItemFlag.ItemIsUserCheckable)

                chk = _CheckCompat(top)

                class _ContainerCompat(QWidget):
                    def __init__(self, c: QCheckBox):
                        super().__init__()
                        self._c = c

                    def findChild(self, cls, *args, **kwargs):
                        return self._c

                return _ContainerCompat(chk)
            return self.itemWidget(top, col)
        return None


class DetailsPanel(QWidget):
    """Collapsible and tabbed bottom panel showing details for the selected download."""

    close_requested = Signal()

    def __init__(self, manager: DownloadManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._manager = manager
        self._download_id: Optional[str] = None
        self._current_entry: Optional[DownloadEntry] = None
        self._files_hash: Optional[tuple] = None
        self._tree_updating: bool = False
        self._file_item_map: dict[int, QTreeWidgetItem] = {}
        self._folder_items: list[QTreeWidgetItem] = []

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
        self._btn_close.setObjectName("detailsCloseBtn")
        self._btn_close.setToolTip("Hide details panel (F4)")
        self._btn_close.setFixedSize(26, 26)
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
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

        self._tree_files = FilesTreeWidget(container)
        self._tree_files.setHeaderLabels([
            "Name", "Size", "Progress", "Priority", "Status"
        ])
        header = self._tree_files.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._tree_files.setColumnWidth(2, 140)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._tree_files.setColumnWidth(3, 130)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._tree_files.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self._tree_files.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)

        self._tree_files.setStyleSheet(f"""
            QTreeWidget {{
                background-color: {Colors.BG_MID};
                color: {Colors.TEXT};
                border: 1px solid {Colors.BORDER};
                font-size: 12px;
            }}
            QTreeWidget::item {{
                padding: 3px 0px;
                border-bottom: 1px solid {Colors.BG_HOVER};
            }}
            QHeaderView::section {{
                background-color: {Colors.BG_DARK};
                color: {Colors.TEXT_SECONDARY};
                padding: 4px 8px;
                font-weight: bold;
                font-size: 11px;
                border: 1px solid {Colors.BORDER};
            }}
        """)

        self._tree_files.itemChanged.connect(self._on_tree_item_changed)
        self._tree_files.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree_files.customContextMenuRequested.connect(self._show_files_context_menu)
        self._table_files = self._tree_files

        layout.addWidget(self._tree_files)
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

        # Peers tab is only relevant for BitTorrent
        is_torrent = (entry.download_type == "torrent")
        peers_tab_idx = self._tabs.indexOf(self._tab_peers)
        if peers_tab_idx != -1:
            self._tabs.setTabVisible(peers_tab_idx, is_torrent)
            if not is_torrent and self._tabs.currentIndex() == peers_tab_idx:
                self._tabs.setCurrentIndex(0)

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
        total_size = entry.total_size
        downloaded_size = entry.downloaded_size
        if entry.status in ("completed", "seeding"):
            if total_size > 0:
                downloaded_size = max(downloaded_size, total_size)
            elif downloaded_size > 0:
                total_size = downloaded_size
            pct = 100.0
        else:
            pct = (downloaded_size / total_size * 100) if total_size > 0 else 0.0

        total_str = humanize.naturalsize(total_size, binary=True) if total_size > 0 else "Unknown"
        dl_str = humanize.naturalsize(downloaded_size, binary=True)
        self._ov_size.setText(f"{total_str} ({pct:.1f}%)")
        self._ov_downloaded.setText(f"{dl_str} / {total_str}")

        # Speeds
        down_speed = _format_speed(entry.speed)
        up_speed = _format_speed(entry.upload_speed)
        if entry.download_type == "torrent":
            self._ov_speed.setText(f"↓ {down_speed}   |   ↑ {up_speed}")
            ts = getattr(entry, "total_seeds", 0) or (entry.metadata.get("total_seeds", 0) if entry.metadata else 0)
            tp = getattr(entry, "total_peers", 0) or (entry.metadata.get("total_peers", 0) if entry.metadata else 0)
            seeds = to_int(entry.seeds)
            peers = to_int(entry.peers)
            ts = to_int(ts)
            tp = to_int(tp)
            s_str = f"{seeds} ({ts})" if ts > seeds else f"{seeds}"
            p_str = f"{peers} ({tp})" if tp > peers else f"{peers}"
            self._ov_swarm.setText(f"{s_str} seeds, {p_str} peers connected")
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
        if not isinstance(files, list) or not files:
            self._files_hash = None
            self._file_item_map.clear()
            self._folder_items.clear()
            self._tree_files.clear()
            return

        is_torrent = (entry.download_type == "torrent")
        structure_hash = tuple((f.get("index", i), str(f.get("path", ""))) for i, f in enumerate(files))
        if self._files_hash != structure_hash:
            self._files_hash = structure_hash
            self._build_files_tree(files, is_torrent)

        self._update_file_values(files, is_torrent)

    def _build_files_tree(self, files: list[dict], is_torrent: bool):
        self._tree_files.blockSignals(True)
        self._tree_files.clear()
        self._file_item_map.clear()
        self._folder_items.clear()

            # Build folder hierarchy
        root_nodes: dict[str, dict] = {}
        for f in files:
            raw_path = str(f.get("path", "file")).replace("\\", "/").strip("/")
            parts = [p for p in raw_path.split("/") if p]
            if not parts:
                parts = ["file"]
            if len(parts) == 1:
                root_nodes[parts[0]] = {"type": "file", "name": parts[0], "data": f}
            else:
                curr = root_nodes
                for p in parts[:-1]:
                    if p not in curr or curr[p]["type"] != "folder":
                        curr[p] = {"type": "folder", "name": p, "children": {}}
                    curr = curr[p]["children"]
                curr[parts[-1]] = {"type": "file", "name": parts[-1], "data": f}

        progress_style = (
            f"QProgressBar {{ border: 1px solid {Colors.BORDER}; border-radius: 3px; background: {Colors.BG_DARK}; height: 16px; text-align: center; font-size: 10px; color: {Colors.TEXT}; }} "
            f"QProgressBar::chunk {{ background: {Colors.ACCENT}; border-radius: 2px; }}"
        )

        def _create_items(parent_widget_or_item, node_dict: dict):
            for name, node in node_dict.items():
                if node["type"] == "folder":
                    item = QTreeWidgetItem(parent_widget_or_item)
                    item.setText(0, f"📁 {name}")
                    item.setData(0, Qt.ItemDataRole.UserRole, {"is_folder": True, "name": name})
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
                    if is_torrent:
                        item.setCheckState(0, Qt.CheckState.Checked)
                    else:
                        item.setCheckState(0, Qt.CheckState.Checked)
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    self._folder_items.append(item)

                    pb = QProgressBar()
                    pb.setRange(0, 100)
                    pb.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    pb.setStyleSheet(progress_style)
                    self._tree_files.setItemWidget(item, 2, pb)

                    if is_torrent:
                        combo = QComboBox()
                        for p_text in ["Max (100%)", "High (75%)", "Medium (50%)", "Low (25%)", "Don't Download"]:
                            combo.addItem(p_text, _PRIORITY_TO_VAL[p_text])
                        combo.setCurrentText("Medium (50%)")
                        combo.currentIndexChanged.connect(lambda idx, it=item: self._on_folder_priority_changed(it))
                        self._tree_files.setItemWidget(item, 3, combo)
                    else:
                        item.setText(3, "Medium (50%)")

                    _create_items(item, node["children"])
                else:
                    f_data = node["data"]
                    f_idx = f_data.get("index", 0)
                    item = QTreeWidgetItem(parent_widget_or_item)
                    item.setText(0, f"📄 {name}")
                    item.setData(0, Qt.ItemDataRole.UserRole, {"is_folder": False, "file_index": f_idx, "data": f_data})
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)

                    curr_prio = f_data.get("priority", 4)
                    is_dl = (curr_prio > 0)
                    if is_torrent:
                        item.setCheckState(0, Qt.CheckState.Checked if is_dl else Qt.CheckState.Unchecked)
                    else:
                        item.setCheckState(0, Qt.CheckState.Checked)
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

                    self._file_item_map[f_idx] = item

                    pb = QProgressBar()
                    pb.setRange(0, 100)
                    pb.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    pb.setStyleSheet(progress_style)
                    self._tree_files.setItemWidget(item, 2, pb)

                    if is_torrent:
                        combo = QComboBox()
                        for p_text in ["Max (100%)", "High (75%)", "Medium (50%)", "Low (25%)", "Don't Download"]:
                            combo.addItem(p_text, _PRIORITY_TO_VAL[p_text])
                        prio_label = _priority_to_label(curr_prio)
                        combo.setCurrentText(prio_label)
                        combo.currentIndexChanged.connect(lambda idx, it=item: self._on_file_priority_combo_changed(it))
                        self._tree_files.setItemWidget(item, 3, combo)
                    else:
                        item.setText(3, "Medium (50%)")

        _create_items(self._tree_files, root_nodes)
        self._tree_files.expandAll()
        self._tree_files.blockSignals(False)

    def _get_descendant_file_items(self, item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        files = []
        for i in range(item.childCount()):
            child = item.child(i)
            data = child.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("is_folder"):
                files.extend(self._get_descendant_file_items(child))
            else:
                files.append(child)
        return files

    def _refresh_folder_aggregates(self, folder_item: QTreeWidgetItem, is_torrent: bool):
        descendant_files = self._get_descendant_file_items(folder_item)
        if not descendant_files:
            return

        total_size = 0
        total_downloaded = 0
        checked_count = 0
        all_completed = True
        any_downloading = False
        any_error = False
        priorities = set()

        for it in descendant_files:
            data = it.data(0, Qt.ItemDataRole.UserRole) or {}
            f = data.get("data", {})
            sz = f.get("size", 0)
            total_size += sz
            pct_raw = f.get("progress", 0.0)
            pct = (pct_raw / 100.0) if pct_raw > 1.0 else pct_raw
            total_downloaded += int(sz * pct)

            if it.checkState(0) == Qt.CheckState.Checked:
                checked_count += 1

            st = f.get("status", "pending")
            if st != "completed":
                all_completed = False
            if st in ("downloading", "fetching_metadata"):
                any_downloading = True
            elif st == "error":
                any_error = True

            priorities.add(f.get("priority", 4))

        folder_item.setText(1, humanize.naturalsize(total_size, binary=True) if total_size > 0 else "—")

        folder_pct = (total_downloaded / total_size * 100.0) if total_size > 0 else 0.0
        pb = self._tree_files.itemWidget(folder_item, 2)
        if isinstance(pb, QProgressBar):
            pb.setValue(int(min(max(folder_pct, 0.0), 100.0)))

        # Update check state
        if checked_count == len(descendant_files):
            new_state = Qt.CheckState.Checked
        elif checked_count == 0:
            new_state = Qt.CheckState.Unchecked
        else:
            new_state = Qt.CheckState.PartiallyChecked
        self._tree_files.blockSignals(True)
        try:
            folder_item.setCheckState(0, new_state)
        finally:
            self._tree_files.blockSignals(False)

        # Update priority combo
        if is_torrent:
            combo = self._tree_files.itemWidget(folder_item, 3)
            if isinstance(combo, QComboBox):
                combo.blockSignals(True)
                if len(priorities) == 1:
                    p_val = next(iter(priorities))
                    combo.setCurrentText(_priority_to_label(p_val))
                else:
                    if combo.findText("Mixed") == -1:
                        combo.addItem("Mixed", -1)
                    combo.setCurrentText("Mixed")
                combo.blockSignals(False)

        # Update status
        if all_completed and len(descendant_files) > 0:
            folder_item.setText(4, "Completed")
        elif any_error:
            folder_item.setText(4, "Error")
        elif any_downloading:
            folder_item.setText(4, "Downloading")
        else:
            folder_item.setText(4, "Pending")

    def _update_file_values(self, files: list[dict], is_torrent: bool):
        for f in files:
            f_idx = f.get("index", 0)
            item = self._file_item_map.get(f_idx)
            if not item:
                continue

            data = item.data(0, Qt.ItemDataRole.UserRole) or {}
            data["data"] = f
            item.setData(0, Qt.ItemDataRole.UserRole, data)

            size_val = f.get("size", 0)
            size_str = humanize.naturalsize(size_val, binary=True) if size_val > 0 else "—"
            item.setText(1, size_str)

            pct_raw = f.get("progress", 0.0)
            pct_val = pct_raw if pct_raw > 1.0 else (pct_raw * 100.0)
            pb = self._tree_files.itemWidget(item, 2)
            if isinstance(pb, QProgressBar):
                pb.setValue(int(min(max(pct_val, 0.0), 100.0)))

            status_str = f.get("status", "pending")
            item.setText(4, _to_str(status_str).capitalize())

            if is_torrent:
                curr_prio = f.get("priority", 4)
                combo = self._tree_files.itemWidget(item, 3)
                if isinstance(combo, QComboBox):
                    combo.blockSignals(True)
                    combo.setCurrentText(_priority_to_label(curr_prio))
                    combo.blockSignals(False)
                expected_state = Qt.CheckState.Checked if curr_prio > 0 else Qt.CheckState.Unchecked
                if item.checkState(0) != expected_state:
                    self._tree_files.blockSignals(True)
                    try:
                        item.setCheckState(0, expected_state)
                    finally:
                        self._tree_files.blockSignals(False)

        for folder_item in reversed(self._folder_items):
            self._refresh_folder_aggregates(folder_item, is_torrent)

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int):
        if column != 0 or self._tree_updating or not self._download_id:
            return
        new_state = item.checkState(0)
        if new_state == Qt.CheckState.PartiallyChecked:
            return
        self._tree_updating = True
        try:
            data = item.data(0, Qt.ItemDataRole.UserRole) or {}
            is_folder = data.get("is_folder", False)
            is_checked = (new_state == Qt.CheckState.Checked)

            if is_folder:
                descendants = self._get_descendant_file_items(item)
                self._tree_files.blockSignals(True)
                try:
                    for f_it in descendants:
                        f_it.setCheckState(0, Qt.CheckState.Checked if is_checked else Qt.CheckState.Unchecked)
                finally:
                    self._tree_files.blockSignals(False)

                for f_it in descendants:
                    f_data = f_it.data(0, Qt.ItemDataRole.UserRole) or {}
                    f_idx = f_data.get("file_index")
                    prio = 4 if is_checked else 0
                    if "data" in f_data and isinstance(f_data["data"], dict):
                        f_data["data"]["priority"] = prio
                    combo = self._tree_files.itemWidget(f_it, 3)
                    if isinstance(combo, QComboBox):
                        combo.blockSignals(True)
                        combo.setCurrentText("Medium (50%)" if is_checked else "Don't Download")
                        combo.blockSignals(False)
                    if f_idx is not None:
                        self._manager.set_torrent_file_priority(self._download_id, f_idx, prio)
            else:
                f_idx = data.get("file_index")
                prio = 4 if is_checked else 0
                if "data" in data and isinstance(data["data"], dict):
                    data["data"]["priority"] = prio
                combo = self._tree_files.itemWidget(item, 3)
                if isinstance(combo, QComboBox):
                    combo.blockSignals(True)
                    combo.setCurrentText("Medium (50%)" if is_checked else "Don't Download")
                    combo.blockSignals(False)
                if f_idx is not None:
                    self._manager.set_torrent_file_priority(self._download_id, f_idx, prio)

            for fld in reversed(self._folder_items):
                self._refresh_folder_aggregates(fld, is_torrent=True)
        finally:
            self._tree_updating = False

    def _on_file_priority_combo_changed(self, item: QTreeWidgetItem):
        if self._tree_updating or not self._download_id:
            return
        combo = self._tree_files.itemWidget(item, 3)
        if not isinstance(combo, QComboBox):
            return
        prio_val = combo.currentData()
        if prio_val is None or prio_val < 0:
            return

        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        f_idx = data.get("file_index")
        if f_idx is None:
            return

        self._tree_updating = True
        try:
            new_state = Qt.CheckState.Checked if prio_val > 0 else Qt.CheckState.Unchecked
            self._tree_files.blockSignals(True)
            try:
                item.setCheckState(0, new_state)
            finally:
                self._tree_files.blockSignals(False)
            if "data" in data and isinstance(data["data"], dict):
                data["data"]["priority"] = prio_val
            self._manager.set_torrent_file_priority(self._download_id, f_idx, prio_val)
            for fld in reversed(self._folder_items):
                self._refresh_folder_aggregates(fld, is_torrent=True)
        finally:
            self._tree_updating = False

    def _on_folder_priority_changed(self, item: QTreeWidgetItem):
        if self._tree_updating or not self._download_id:
            return
        combo = self._tree_files.itemWidget(item, 3)
        if not isinstance(combo, QComboBox):
            return
        prio_val = combo.currentData()
        if prio_val is None or prio_val < 0:
            return

        self._tree_updating = True
        try:
            descendants = self._get_descendant_file_items(item)
            for f_it in descendants:
                f_data = f_it.data(0, Qt.ItemDataRole.UserRole) or {}
                f_idx = f_data.get("file_index")
                new_state = Qt.CheckState.Checked if prio_val > 0 else Qt.CheckState.Unchecked
                f_it.setCheckState(0, new_state)
                f_combo = self._tree_files.itemWidget(f_it, 3)
                if isinstance(f_combo, QComboBox):
                    f_combo.blockSignals(True)
                    f_combo.setCurrentText(_priority_to_label(prio_val))
                    f_combo.blockSignals(False)
                if f_idx is not None:
                    self._manager.set_torrent_file_priority(self._download_id, f_idx, prio_val)

            for fld in reversed(self._folder_items):
                self._refresh_folder_aggregates(fld, is_torrent=True)
        finally:
            self._tree_updating = False

    def _show_files_context_menu(self, pos):
        item = self._tree_files.itemAt(pos)
        if not item or not self._download_id:
            return
        entry = self._manager.get_entry(self._download_id)
        if not entry or entry.download_type != "torrent":
            return

        menu = QMenu(self)
        prio_menu = menu.addMenu("Bandwidth Allocation / Priority")
        options = [
            ("Max (100%)", 7),
            ("High (75%)", 6),
            ("Medium (50%)", 4),
            ("Low (25%)", 1),
            ("Don't Download", 0),
        ]
        combo = self._tree_files.itemWidget(item, 3)
        curr_text = combo.currentText() if isinstance(combo, QComboBox) else ""

        for text, val in options:
            act = prio_menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(curr_text == text)
            act.triggered.connect(lambda checked=False, v=val, it=item: self._set_item_priority(it, v))

        menu.exec(self._tree_files.viewport().mapToGlobal(pos))

    def _set_item_priority(self, item: QTreeWidgetItem, priority_val: int):
        combo = self._tree_files.itemWidget(item, 3)
        if isinstance(combo, QComboBox):
            idx = combo.findData(priority_val)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _on_row_checkbox_toggled(self, row: int, checked: bool):
        if not self._download_id:
            return
        if row in self._file_item_map:
            item = self._file_item_map[row]
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._on_tree_item_changed(item, 0)

    def _on_row_priority_changed(self, row: int):
        if not self._download_id:
            return
        if row in self._file_item_map:
            item = self._file_item_map[row]
            self._on_file_priority_combo_changed(item)

    def _on_file_checkbox_toggled(self, file_index: int, checked: bool, combo: Optional[QComboBox]):
        if not self._download_id:
            return
        prio = 4 if checked else 0
        if combo:
            combo.blockSignals(True)
            combo.setCurrentText("Normal" if checked else "Don't Download")
            combo.blockSignals(False)
        self._manager.set_torrent_file_priority(self._download_id, file_index, prio)

    def _on_file_priority_changed(self, file_index: int, combo: QComboBox, chk: Optional[QCheckBox] = None):
        if not self._download_id:
            return
        prio_val = combo.currentData()
        if prio_val is not None:
            if chk:
                chk.blockSignals(True)
                chk.setChecked(prio_val > 0)
                chk.blockSignals(False)
            self._manager.set_torrent_file_priority(self._download_id, file_index, prio_val)

    def _update_peers(self, entry: DownloadEntry):
        if entry.download_type != "torrent":
            self._lbl_peers_status.setText("Peer and swarm monitoring is only active for BitTorrent transfers.")
            self._table_peers.setRowCount(0)
            return

        peers = self._manager.get_torrent_peers(entry.id)
        if not isinstance(peers, list):
            peers = []
        ts = getattr(entry, "total_seeds", 0) or (entry.metadata.get("total_seeds", 0) if entry.metadata else 0)
        tp = getattr(entry, "total_peers", 0) or (entry.metadata.get("total_peers", 0) if entry.metadata else 0)
        swarm_str = f" ({ts} seeds, {tp} peers in swarm)" if (ts > 0 or tp > 0) else ""
        self._lbl_peers_status.setText(f"{len(peers)} connected peer(s) in active swarm{swarm_str}")

        rebuild = self._table_peers.rowCount() != len(peers)
        if rebuild:
            self._table_peers.setRowCount(len(peers))

        for row, p in enumerate(peers):
            if not isinstance(p, dict):
                continue
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
            c_name = _to_str(p.get("client", "Unknown")).strip()
            client_item.setText(c_name if c_name else "Unknown")

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
        if not isinstance(trackers, list):
            trackers = []
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

    # -- State Persistence ----------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Return serializable state of the details panel (active tab index, etc.)."""
        return {
            "current_tab": self._tabs.currentIndex(),
        }

    def restore_state(self, state: dict[str, Any]):
        """Restore serializable state of the details panel."""
        if not isinstance(state, dict):
            return
        tab_idx = state.get("current_tab")
        if tab_idx is not None:
            try:
                idx = int(tab_idx)
                if 0 <= idx < self._tabs.count():
                    self._tabs.setCurrentIndex(idx)
            except (ValueError, TypeError):
                pass
