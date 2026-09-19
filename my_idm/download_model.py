"""QAbstractTableModel for the download list view."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import humanize
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor

from my_idm.config import TorConfig
from my_idm.database import DownloadEntry
from my_idm.styles import Colors


# Column definitions
class Col:
    NAME = 0
    SIZE = 1
    PROGRESS = 2
    STATUS = 3
    SPEED = 4
    ETA = 5
    TYPE = 6
    SEEDS_PEERS = 7
    ADDED = 8
    LAST_TRIED = 9
    COMPLETED = 10
    SAVE_PATH = 11

    HEADERS = [
        "Name", "Size", "Progress", "Status", "Speed", "ETA",
        "Type", "Seeds / Peers", "Added", "Last Tried", "Completed",
        "Save Path",
    ]
    COUNT = len(HEADERS)


_STATUS_COLORS = {
    "downloading": QColor(Colors.ACCENT),
    "completed":   QColor(Colors.GREEN),
    "seeding":     QColor(Colors.PURPLE),
    "paused":      QColor(Colors.ORANGE),
    "error":       QColor(Colors.RED),
    "queued":      QColor(Colors.TEXT_DIM),
    "checking":    QColor(Colors.ORANGE),
    "scanning":    QColor(Colors.CYAN),
    "threat_detected": QColor(Colors.RED),
}


def _format_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    return f"{humanize.naturalsize(bps, binary=True)}/s"


def _format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m"


def _format_time(iso_str: str) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        # Convert to local time for display
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16] if len(iso_str) >= 16 else iso_str


class DownloadTableModel(QAbstractTableModel):
    """Table model backed by a list of DownloadEntry objects."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[DownloadEntry] = []
        self._id_to_row: dict[str, int] = {}
        self._sort_column: int = Col.ADDED
        self._sort_order: Qt.SortOrder = Qt.SortOrder.DescendingOrder
        self._tor_config: Optional[TorConfig] = None

    @property
    def tor_config(self) -> Optional[TorConfig]:
        return self._tor_config

    def set_tor_config(self, config: Optional[TorConfig]):
        """Update Tor configuration and notify view to repaint rows immediately."""
        self._tor_config = config
        if self._entries:
            left = self.index(0, 0)
            right = self.index(len(self._entries) - 1, Col.COUNT - 1)
            self.dataChanged.emit(
                left,
                right,
                [
                    Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.ForegroundRole,
                    Qt.ItemDataRole.ToolTipRole,
                ],
            )

    def is_tor_active_for(self, entry: DownloadEntry) -> bool:
        """Return True if this download is actively transferring over Tor right now."""
        if not self._tor_config or not self._tor_config.enabled:
            return False
        if entry.download_type == "http":
            return entry.status == "downloading" and self._tor_config.route_http
        if entry.download_type == "torrent":
            return entry.status in ("downloading", "seeding") and self._tor_config.route_torrent
        return False

    @property
    def sort_column(self) -> int:
        return self._sort_column

    @property
    def sort_order(self) -> Qt.SortOrder:
        return self._sort_order

    # -- data population -----------------------------------------------------

    def load_entries(self, entries: list[DownloadEntry]):
        self.beginResetModel()
        self._entries = list(entries)
        if self._sort_column is not None:
            self._apply_sort()
        self._rebuild_index()
        self.endResetModel()

    def add_entry(self, entry: DownloadEntry):
        row = self._find_insert_row(entry)
        self.beginInsertRows(QModelIndex(), row, row)
        self._entries.insert(row, entry)
        self._rebuild_index()
        self.endInsertRows()

    def remove_entry(self, download_id: str):
        row = self._id_to_row.get(download_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._entries.pop(row)
        self._rebuild_index()
        self.endRemoveRows()

    # -- sorting -------------------------------------------------------------

    def sort(self, column: int, order: Optional[Qt.SortOrder] = None):
        """Sort the model by the specified column and order."""
        if order is None:
            order = Qt.SortOrder.DescendingOrder if column == Col.ADDED else Qt.SortOrder.AscendingOrder
        self._sort_column = column
        self._sort_order = order
        if not self._entries:
            return

        self.layoutAboutToBeChanged.emit()
        old_ids = [e.id for e in self._entries]
        self._apply_sort()
        self._rebuild_index()

        old_indexes = self.persistentIndexList()
        new_indexes = []
        for idx in old_indexes:
            if idx.row() < len(old_ids):
                old_id = old_ids[idx.row()]
                new_row = self._id_to_row.get(old_id, idx.row())
                new_indexes.append(self.index(new_row, idx.column()))
            else:
                new_indexes.append(idx)
        self.changePersistentIndexList(old_indexes, new_indexes)
        self.layoutChanged.emit()

    def _apply_sort(self):
        if self._sort_column is None:
            return
        ascending = (
            self._sort_order == Qt.SortOrder.AscendingOrder
            or self._sort_order == 0
        )
        reverse = not ascending
        self._entries.sort(
            key=lambda e: self._entry_sort_key(e, self._sort_column, ascending),
            reverse=reverse,
        )

    def _entry_sort_key(self, entry: DownloadEntry, col: int, ascending: bool) -> Any:
        if col == Col.NAME:
            return (entry.filename or entry.url or "").lower()

        if col == Col.SIZE:
            return entry.total_size if entry.total_size > 0 else -1

        if col == Col.PROGRESS:
            return entry.progress

        if col == Col.STATUS:
            return (entry.status or "").lower()

        if col == Col.SPEED:
            if entry.status == "downloading":
                return entry.speed
            if entry.status == "seeding":
                return entry.upload_speed
            return 0.0

        if col == Col.ETA:
            # Active downloads with ETA first; inactive ("—") at bottom
            has_eta = entry.status == "downloading" and entry.eta_seconds > 0
            if ascending:
                return (0, entry.eta_seconds) if has_eta else (1, 0.0)
            else:
                return (1, entry.eta_seconds) if has_eta else (0, 0.0)

        if col == Col.TYPE:
            return (entry.download_type or "").lower()

        if col == Col.SEEDS_PEERS:
            if entry.download_type == "torrent":
                return (entry.seeds, entry.peers)
            if entry.download_type == "http":
                return (entry.num_segments, 0)
            return (0, 0)

        if col == Col.ADDED:
            has_time = bool(entry.added_at)
            if ascending:
                return (0, entry.added_at) if has_time else (1, "")
            else:
                return (1, entry.added_at) if has_time else (0, "")

        if col == Col.LAST_TRIED:
            has_time = bool(entry.last_tried_at)
            if ascending:
                return (0, entry.last_tried_at) if has_time else (1, "")
            else:
                return (1, entry.last_tried_at) if has_time else (0, "")

        if col == Col.COMPLETED:
            has_time = bool(entry.completed_at)
            if ascending:
                return (0, entry.completed_at) if has_time else (1, "")
            else:
                return (1, entry.completed_at) if has_time else (0, "")

        if col == Col.SAVE_PATH:
            return (entry.save_path or "").lower()

        return ""

    def _find_insert_row(self, entry: DownloadEntry) -> int:
        if self._sort_column is None or not self._entries:
            return len(self._entries)
        ascending = (
            self._sort_order == Qt.SortOrder.AscendingOrder
            or self._sort_order == 0
        )
        reverse = not ascending
        key = self._entry_sort_key(entry, self._sort_column, ascending)
        for i, existing in enumerate(self._entries):
            existing_key = self._entry_sort_key(existing, self._sort_column, ascending)
            if reverse:
                if key > existing_key:
                    return i
            else:
                if key < existing_key:
                    return i
        return len(self._entries)

    def get_entry(self, row: int) -> Optional[DownloadEntry]:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def get_entry_by_id(self, download_id: str) -> Optional[DownloadEntry]:
        row = self._id_to_row.get(download_id)
        if row is not None and 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def get_selected_ids(self, indexes: list[QModelIndex]) -> list[str]:
        rows = sorted(set(idx.row() for idx in indexes))
        return [
            self._entries[r].id for r in rows
            if 0 <= r < len(self._entries)
        ]

    @property
    def entries(self) -> list[DownloadEntry]:
        return self._entries

    def get_aggregate_speeds(self) -> tuple[float, float]:
        """Returns (total_download_speed, total_upload_speed) in B/s."""
        down = sum(e.speed for e in self._entries if e.status == "downloading")
        up = sum(e.upload_speed for e in self._entries if e.status in ("downloading", "seeding"))
        return down, up

    # -- progress updates (called from manager signals) ---------------------

    def update_progress(self, download_id: str, downloaded: int,
                        total: int, speed: float, eta: float,
                        seeds: int = 0, peers: int = 0,
                        upload_speed: float = 0.0):
        row = self._id_to_row.get(download_id)
        if row is None:
            return
        entry = self._entries[row]
        # Guard against minor backwards jitter during active download from out-of-order signals
        if (
            downloaded < entry.downloaded_size
            and entry.status == "downloading"
            and entry.total_size > 0
            and total == entry.total_size
            and (entry.downloaded_size - downloaded < 1024 * 1024)
            and downloaded > 0
        ):
            return

        entry.downloaded_size = downloaded
        if total > 0:
            entry.total_size = total
        entry.speed = speed
        entry.eta_seconds = eta
        entry.seeds = seeds
        entry.peers = peers
        entry.upload_speed = upload_speed

        # Emit change for relevant columns
        left = self.index(row, Col.SIZE)
        right = self.index(row, Col.SEEDS_PEERS)
        self.dataChanged.emit(left, right, [Qt.ItemDataRole.DisplayRole])

    def update_status(self, download_id: str, status: str,
                      error_msg: str = ""):
        row = self._id_to_row.get(download_id)
        if row is None:
            return
        entry = self._entries[row]
        entry.status = status
        entry.error_message = error_msg

        if status == "completed":
            entry.speed = 0
            entry.eta_seconds = 0

        left = self.index(row, 0)
        right = self.index(row, Col.COUNT - 1)
        self.dataChanged.emit(
            left, right,
            [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ForegroundRole,
                Qt.ItemDataRole.ToolTipRole,
            ],
        )

    def update_filename(self, download_id: str, filename: str):
        """Update filename when resolved from server headers or metadata."""
        row = self._id_to_row.get(download_id)
        if row is None:
            return
        entry = self._entries[row]
        entry.filename = filename
        if entry.save_path:
            entry.file_path = str(Path(entry.save_path) / filename)

        left = self.index(row, Col.NAME)
        right = self.index(row, Col.SAVE_PATH)
        self.dataChanged.emit(
            left, right,
            [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole],
        )

    def refresh_entry(self, download_id: str, entry: DownloadEntry):
        """Full refresh of an entry (e.g. after move or recheck)."""
        row = self._id_to_row.get(download_id)
        if row is None:
            return
        self._entries[row] = entry
        left = self.index(row, 0)
        right = self.index(row, Col.COUNT - 1)
        self.dataChanged.emit(left, right)

    # -- QAbstractTableModel interface ---------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return Col.COUNT

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return Col.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()
        if row < 0 or row >= len(self._entries):
            return None

        entry = self._entries[row]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_data(entry, col)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == Col.STATUS:
                if self.is_tor_active_for(entry):
                    return QColor(Colors.PURPLE)
                return _STATUS_COLORS.get(entry.status, QColor(Colors.TEXT))

        if role == Qt.ItemDataRole.ToolTipRole:
            is_tor = self.is_tor_active_for(entry)
            tor_note = ""
            if is_tor and self._tor_config:
                tor_note = f"🧅 Active Tor Route: Routed via SOCKS5 proxy ({self._tor_config.socks5_url})"

            if col == Col.NAME:
                base = entry.file_path or entry.url
                return f"{tor_note}\n{base}".strip() if tor_note else base
            if col == Col.STATUS:
                if entry.error_message:
                    return f"{tor_note}\n{entry.error_message}".strip() if tor_note else entry.error_message
                if is_tor and self._tor_config:
                    return f"Active Tor Transfer: Routed via SOCKS5 proxy ({self._tor_config.socks5_url})"
            if col == Col.TYPE and is_tor:
                return f"Traffic routed via Tor SOCKS5 proxy ({self._tor_config.socks5_url})"

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )

    # -- display helpers -----------------------------------------------------

    def _display_data(self, entry: DownloadEntry, col: int) -> Any:
        if col == Col.NAME:
            raw_name = entry.filename or entry.url[:60]
            if self.is_tor_active_for(entry):
                return f"🧅 {raw_name}"
            return raw_name

        if col == Col.SIZE:
            if entry.total_size > 0:
                return humanize.naturalsize(entry.total_size, binary=True)
            return "—"

        if col == Col.PROGRESS:
            # Return dict for ProgressBarDelegate
            return {
                "progress": entry.progress,
                "status": entry.status,
            }

        if col == Col.STATUS:
            if entry.status == "threat_detected":
                return "Threat Detected ⚠"
            if entry.status == "scanning":
                return "Scanning 🛡️"
            s = entry.status.capitalize()
            if entry.status == "error" and entry.error_message:
                s += f" ⚠"
            if entry.status == "queued" and entry.retry_count > 0:
                s += f" (retry {entry.retry_count})"
            if self.is_tor_active_for(entry):
                s += " (Tor 🧅)"
            return s

        if col == Col.SPEED:
            if entry.download_type == "torrent":
                if entry.status in ("downloading", "seeding"):
                    return f"↓ {_format_speed(entry.speed)}  ↑ {_format_speed(entry.upload_speed)}"
                return "—"
            if entry.status == "downloading":
                return _format_speed(entry.speed)
            if entry.status == "seeding":
                return f"↑ {_format_speed(entry.upload_speed)}"
            return "—"

        if col == Col.ETA:
            if entry.status == "downloading":
                return _format_eta(entry.eta_seconds)
            return "—"

        if col == Col.TYPE:
            t = entry.download_type.upper()
            if self.is_tor_active_for(entry):
                return f"🧅 {t}"
            return t

        if col == Col.SEEDS_PEERS:
            if entry.download_type == "torrent":
                return f"S:{entry.seeds}  P:{entry.peers}"
            if entry.download_type == "http":
                return f"{entry.num_segments} seg"
            return "—"

        if col == Col.ADDED:
            return _format_time(entry.added_at)

        if col == Col.LAST_TRIED:
            return _format_time(entry.last_tried_at)

        if col == Col.COMPLETED:
            return _format_time(entry.completed_at)

        if col == Col.SAVE_PATH:
            return entry.save_path or "—"

        return None

    # -- index management ----------------------------------------------------

    def _rebuild_index(self):
        self._id_to_row = {
            e.id: i for i, e in enumerate(self._entries)
        }
