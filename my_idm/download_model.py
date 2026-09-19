"""QAbstractTableModel for the download list view."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import humanize
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor

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

    # -- data population -----------------------------------------------------

    def load_entries(self, entries: list[DownloadEntry]):
        self.beginResetModel()
        self._entries = list(entries)
        self._rebuild_index()
        self.endResetModel()

    def add_entry(self, entry: DownloadEntry):
        row = len(self._entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self._entries.append(entry)
        self._id_to_row[entry.id] = row
        self.endInsertRows()

    def remove_entry(self, download_id: str):
        row = self._id_to_row.get(download_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._entries.pop(row)
        self._rebuild_index()
        self.endRemoveRows()

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

    # -- progress updates (called from manager signals) ---------------------

    def update_progress(self, download_id: str, downloaded: int,
                        total: int, speed: float, eta: float,
                        seeds: int = 0, peers: int = 0,
                        upload_speed: float = 0.0):
        row = self._id_to_row.get(download_id)
        if row is None:
            return
        entry = self._entries[row]
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

        idx = self.index(row, Col.STATUS)
        self.dataChanged.emit(
            idx, self.index(row, Col.COUNT - 1),
            [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole],
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
                return _STATUS_COLORS.get(entry.status, QColor(Colors.TEXT))

        if role == Qt.ItemDataRole.ToolTipRole:
            if col == Col.NAME:
                return entry.file_path or entry.url
            if col == Col.STATUS and entry.error_message:
                return entry.error_message

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )

    # -- display helpers -----------------------------------------------------

    def _display_data(self, entry: DownloadEntry, col: int) -> Any:
        if col == Col.NAME:
            return entry.filename or entry.url[:60]

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
            s = entry.status.capitalize()
            if entry.status == "error" and entry.error_message:
                s += f" ⚠"
            if entry.status == "queued" and entry.retry_count > 0:
                s += f" (retry {entry.retry_count})"
            return s

        if col == Col.SPEED:
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
            return entry.download_type.upper()

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
