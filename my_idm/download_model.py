"""QAbstractTableModel for the download list view."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import humanize
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QFont

from my_idm.config import TorConfig
from my_idm.database import DownloadEntry
from my_idm.styles import Colors
from my_idm.utils import create_emoji_icon, extract_source_domain, normalize_path, to_int

_ICON_CACHE: dict[str, Any] = {}

def _get_icon(emoji: str):
    if emoji not in _ICON_CACHE:
        _ICON_CACHE[emoji] = create_emoji_icon(emoji, size=24)
    return _ICON_CACHE[emoji]


# Column definitions
class Col:
    QUEUE = 0
    NAME = 1
    SOURCE_DOMAIN = 2
    SIZE = 3
    PROGRESS = 4
    STATUS = 5
    SPEED = 6
    ETA = 7
    SEEDS_PEERS = 8
    ADDED = 9
    LAST_TRIED = 10
    COMPLETED = 11
    SAVE_PATH = 12
    FILE_NAME = 13

    DATE_COLUMNS = (ADDED, LAST_TRIED, COMPLETED)

    HEADERS = [
        "#", "Name", "Source Domain", "Size", "Progress", "Status", "Speed", "ETA",
        "Seeds / Peers", "Added", "Last Tried", "Completed",
        "Save Path", "File / Folder Name",
    ]
    COUNT = len(HEADERS)


_STATUS_COLORS = {
    "downloading":       QColor(Colors.ACCENT),
    "completed":         QColor(Colors.GREEN),
    "seeding":           QColor(Colors.PURPLE),
    "paused":            QColor(Colors.ORANGE),
    "error":             QColor(Colors.RED),
    "queued":            QColor(Colors.TEXT_DIM),
    "checking":          QColor(Colors.ORANGE),
    "scanning":          QColor(Colors.CYAN),
    "threat_detected":   QColor(Colors.RED),
    "fetching_metadata": QColor(Colors.CYAN),
    "file_not_found":    QColor(Colors.RED),
    "stalled":           QColor(Colors.ORANGE),
    "stopped":           QColor(Colors.RED),
    "suspended":         QColor(Colors.TEXT_DIM),
}

ACTIVE_QUEUE_STATUSES = {
    "queued", "downloading", "fetching_metadata", "stalled", "checking", "scanning"
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


STATUS_FILTER_GROUPS: dict[str, set[str]] = {
    "downloading": {"downloading", "fetching_metadata", "checking", "scanning", "stalled"},
    "queued": {"queued"},
    "paused": {"paused"},
    "stopped": {"stopped"},
    "suspended": {"suspended"},
    "completed": {"completed"},
    "seeding": {"seeding"},
    "error": {"error", "threat_detected", "file_not_found"},
}

STATUS_FILTER_LABELS: dict[str, str] = {
    "downloading": "Downloading",
    "queued": "Queued",
    "paused": "Paused",
    "stopped": "Stopped",
    "suspended": "Suspended",
    "completed": "Completed",
    "seeding": "Seeding",
    "error": "Error",
}

TYPE_FILTER_LABELS: dict[str, str] = {
    "http": "HTTP / Multi-Segment",
    "torrent": "BitTorrent Swarm",
}

SECTION_ACTIVE = "active"
SECTION_SEEDING = "seeding"
SECTION_INACTIVE = "inactive"

ACTIVE_SECTION_STATUSES = {
    "fetching_metadata", "queued", "downloading", "paused", "stalled", "error",
    "checking", "scanning", "threat_detected"
}
SEEDING_SECTION_STATUSES = {"seeding"}
INACTIVE_SECTION_STATUSES = {"completed", "stopped", "file_not_found", "suspended"}


class DownloadTableModel(QAbstractTableModel):
    """Table model backed by a list of DownloadEntry objects with filtering support."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_entries: list[DownloadEntry] = []
        self._entries: list[DownloadEntry] = []
        self._id_to_row: dict[str, int] = {}
        self._sort_column: int = Col.ADDED
        self._sort_order: Qt.SortOrder = Qt.SortOrder.DescendingOrder
        self._tor_config: Optional[TorConfig] = None
        self._status_filter: Optional[set[str]] = None
        self._type_filter: Optional[set[str]] = None
        self._segregated_view: bool = False
        self._collapsed_sections: set[str] = set()

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

    # -- helpers & filtering ---------------------------------------------------

    _to_int = staticmethod(to_int)

    def _matches_filter(self, entry: DownloadEntry) -> bool:
        if self._type_filter is not None:
            dtype = entry.download_type or "http"
            if dtype not in self._type_filter:
                return False
        if self._status_filter is not None:
            matched = False
            for group in self._status_filter:
                if entry.status in STATUS_FILTER_GROUPS.get(group, {group}):
                    matched = True
                    break
            if not matched:
                return False
        return True

    def _reapply_filter(self):
        self.beginResetModel()
        self._apply_sort()
        self._rebuild_index()
        self.endResetModel()

    def set_segregated_view(self, enabled: bool):
        if self._segregated_view == enabled:
            return
        self._segregated_view = enabled
        self._reapply_filter()

    def is_segregated_view(self) -> bool:
        return self._segregated_view

    def set_section_collapsed(self, section_id: str, collapsed: bool):
        if collapsed:
            self._collapsed_sections.add(section_id)
        else:
            self._collapsed_sections.discard(section_id)
        self._reapply_filter()

    def is_section_collapsed(self, section_id: str) -> bool:
        return section_id in self._collapsed_sections

    def is_section_header_row(self, row: int) -> bool:
        if 0 <= row < len(self._entries):
            return bool(getattr(self._entries[row], "is_section_header", False))
        return False

    def get_section_header_row_indices(self) -> list[int]:
        return [i for i, e in enumerate(self._entries) if getattr(e, "is_section_header", False)]

    def toggle_section_collapsed(self, row: int) -> Optional[tuple[str, bool]]:
        if 0 <= row < len(self._entries):
            e = self._entries[row]
            if getattr(e, "is_section_header", False):
                sec_id = e.section_id
                now_collapsed = sec_id not in self._collapsed_sections
                if now_collapsed:
                    self._collapsed_sections.add(sec_id)
                else:
                    self._collapsed_sections.discard(sec_id)
                self._reapply_filter()
                return (sec_id, now_collapsed)
        return None

    def status_filter(self) -> Optional[set[str]]:
        return self._status_filter

    def type_filter(self) -> Optional[set[str]]:
        return self._type_filter

    def is_filtered(self) -> bool:
        return self._status_filter is not None or self._type_filter is not None

    def is_status_filtered(self) -> bool:
        return self._status_filter is not None

    def is_type_filtered(self) -> bool:
        return self._type_filter is not None

    def set_status_filter(self, allowed_groups: Optional[set[str]]):
        if allowed_groups is not None and len(allowed_groups) >= len(STATUS_FILTER_GROUPS):
            allowed_groups = None
        if self._status_filter == allowed_groups:
            return
        self._status_filter = set(allowed_groups) if allowed_groups is not None else None
        self._reapply_filter()

    def set_type_filter(self, allowed_types: Optional[set[str]]):
        if allowed_types is not None and len(allowed_types) >= len(TYPE_FILTER_LABELS):
            allowed_types = None
        if self._type_filter == allowed_types:
            return
        self._type_filter = set(allowed_types) if allowed_types is not None else None
        self._reapply_filter()

    def clear_filters(self):
        if self._status_filter is None and self._type_filter is None:
            return
        self._status_filter = None
        self._type_filter = None
        self._reapply_filter()

    def total_unfiltered_count(self) -> int:
        return len(self._all_entries)

    def visible_download_count(self) -> int:
        return sum(1 for e in self._entries if not getattr(e, "is_section_header", False))

    def get_status_counts(self) -> dict[str, int]:
        counts = {k: 0 for k in STATUS_FILTER_GROUPS}
        for e in self._all_entries:
            if self._type_filter is not None:
                dtype = e.download_type or "http"
                if dtype not in self._type_filter:
                    continue
            for group, statuses in STATUS_FILTER_GROUPS.items():
                if e.status in statuses:
                    counts[group] += 1
                    break
        return counts

    def get_type_counts(self) -> dict[str, int]:
        counts = {k: 0 for k in TYPE_FILTER_LABELS}
        for e in self._all_entries:
            if self._status_filter is not None:
                matched = any(e.status in STATUS_FILTER_GROUPS.get(g, set()) for g in self._status_filter)
                if not matched:
                    continue
            dtype = e.download_type or "http"
            if dtype in counts:
                counts[dtype] += 1
        return counts

    # -- data population -----------------------------------------------------

    def load_entries(self, entries: list[DownloadEntry]):
        self.beginResetModel()
        self._all_entries = list(entries)
        for e in self._all_entries:
            if e.download_type == "torrent" and e.metadata:
                if not e.seeds and "seeds" in e.metadata:
                    e.seeds = self._to_int(e.metadata.get("seeds", 0))
                if not e.peers and "peers" in e.metadata:
                    e.peers = self._to_int(e.metadata.get("peers", 0))
                if not getattr(e, "total_seeds", 0) and "total_seeds" in e.metadata:
                    e.total_seeds = self._to_int(e.metadata.get("total_seeds", 0))
                if not getattr(e, "total_peers", 0) and "total_peers" in e.metadata:
                    e.total_peers = self._to_int(e.metadata.get("total_peers", 0))
            if e.status in ("completed", "seeding") and e.total_size > 0:
                if e.downloaded_size < e.total_size:
                    e.downloaded_size = e.total_size
        self._apply_sort()
        self._rebuild_index()
        self.endResetModel()

    def add_entry(self, entry: DownloadEntry):
        if entry.download_type == "torrent" and entry.metadata:
            if not entry.seeds and "seeds" in entry.metadata:
                entry.seeds = self._to_int(entry.metadata.get("seeds", 0))
            if not entry.peers and "peers" in entry.metadata:
                entry.peers = self._to_int(entry.metadata.get("peers", 0))
            if not getattr(entry, "total_seeds", 0) and "total_seeds" in entry.metadata:
                entry.total_seeds = self._to_int(entry.metadata.get("total_seeds", 0))
            if not getattr(entry, "total_peers", 0) and "total_peers" in entry.metadata:
                entry.total_peers = self._to_int(entry.metadata.get("total_peers", 0))
        self._all_entries.append(entry)
        if self._segregated_view:
            self._reapply_filter()
            return
        if self._matches_filter(entry):
            row = self._find_insert_row(entry)
            self.beginInsertRows(QModelIndex(), row, row)
            self._entries.insert(row, entry)
            self._rebuild_index()
            self.endInsertRows()

    def remove_entry(self, download_id: str):
        self._all_entries = [e for e in self._all_entries if e.id != download_id]
        if self._segregated_view:
            self._reapply_filter()
            return
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
        filtered = [e for e in self._all_entries if self._matches_filter(e)]
        ascending = (
            self._sort_order == Qt.SortOrder.AscendingOrder
            or self._sort_order == 0
        )
        reverse = not ascending

        if not self._segregated_view:
            if self._sort_column is not None:
                filtered.sort(
                    key=lambda e: self._entry_sort_key(e, self._sort_column, ascending),
                    reverse=reverse,
                )
            self._entries = filtered
            return

        # Segregated view: group by Active, Seeding, Inactive
        active_entries = [e for e in filtered if e.status in ACTIVE_SECTION_STATUSES]
        seeding_entries = [e for e in filtered if e.status in SEEDING_SECTION_STATUSES]
        inactive_entries = [e for e in filtered if e.status in INACTIVE_SECTION_STATUSES]

        if self._sort_column is not None:
            active_entries.sort(
                key=lambda e: self._entry_sort_key(e, self._sort_column, ascending),
                reverse=reverse,
            )
            seeding_entries.sort(
                key=lambda e: self._entry_sort_key(e, self._sort_column, ascending),
                reverse=reverse,
            )
            inactive_entries.sort(
                key=lambda e: self._entry_sort_key(e, self._sort_column, ascending),
                reverse=reverse,
            )

        entries: list[DownloadEntry] = []
        # 1. Active Section
        active_hdr = DownloadEntry(
            id="__section_active__",
            is_section_header=True,
            section_id=SECTION_ACTIVE,
            section_title="Active",
            section_count=len(active_entries),
            section_collapsed=(SECTION_ACTIVE in self._collapsed_sections),
        )
        entries.append(active_hdr)
        if SECTION_ACTIVE not in self._collapsed_sections:
            entries.extend(active_entries)

        # 2. Seeding Section
        seeding_hdr = DownloadEntry(
            id="__section_seeding__",
            is_section_header=True,
            section_id=SECTION_SEEDING,
            section_title="Seeding",
            section_count=len(seeding_entries),
            section_collapsed=(SECTION_SEEDING in self._collapsed_sections),
        )
        entries.append(seeding_hdr)
        if SECTION_SEEDING not in self._collapsed_sections:
            entries.extend(seeding_entries)

        # 3. Inactive Section
        inactive_hdr = DownloadEntry(
            id="__section_inactive__",
            is_section_header=True,
            section_id=SECTION_INACTIVE,
            section_title="Inactive",
            section_count=len(inactive_entries),
            section_collapsed=(SECTION_INACTIVE in self._collapsed_sections),
        )
        entries.append(inactive_hdr)
        if SECTION_INACTIVE not in self._collapsed_sections:
            entries.extend(inactive_entries)

        self._entries = entries

    def _entry_sort_key(self, entry: DownloadEntry, col: int, ascending: bool) -> Any:
        if col == Col.QUEUE:
            is_active = entry.status in ACTIVE_QUEUE_STATUSES
            val = entry.queue_order if entry.queue_order > 0 else 999999
            if ascending:
                return (0, val) if is_active else (1, val)
            else:
                return (1, val) if is_active else (0, val)

        if col == Col.NAME:
            return self.get_original_name(entry).lower()

        if col == Col.SOURCE_DOMAIN:
            return extract_source_domain(entry.url).lower()

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

        if col == Col.SEEDS_PEERS:
            if entry.download_type == "torrent":
                return (to_int(entry.seeds), to_int(entry.peers))
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

        if col == Col.FILE_NAME:
            return self.get_actual_name(entry).lower()

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
            e = self._entries[row]
            return None if getattr(e, "is_section_header", False) else e
        return None

    def get_entry_by_id(self, download_id: str) -> Optional[DownloadEntry]:
        row = self._id_to_row.get(download_id)
        if row is not None and 0 <= row < len(self._entries):
            return self._entries[row]
        for e in self._all_entries:
            if e.id == download_id:
                return e
        return None

    def get_selected_ids(self, indexes: list[QModelIndex]) -> list[str]:
        rows = sorted(set(idx.row() for idx in indexes))
        return [
            self._entries[r].id for r in rows
            if 0 <= r < len(self._entries) and not getattr(self._entries[r], "is_section_header", False)
        ]

    @property
    def entries(self) -> list[DownloadEntry]:
        return self._entries

    @property
    def all_entries(self) -> list[DownloadEntry]:
        return self._all_entries

    def get_aggregate_speeds(self) -> tuple[float, float]:
        """Returns (total_download_speed, total_upload_speed) in B/s."""
        down = sum(e.speed for e in self._all_entries if e.status == "downloading")
        up = sum(e.upload_speed for e in self._all_entries if e.status in ("downloading", "seeding"))
        return down, up

    # -- progress updates (called from manager signals) ---------------------

    def update_progress(self, download_id: str, downloaded: int,
                        total: int, speed: float, eta: float,
                        seeds: int = 0, peers: int = 0,
                        upload_speed: float = 0.0,
                        total_seeds: int = 0, total_peers: int = 0):
        # Update canonical entry in _all_entries
        for e in self._all_entries:
            if e.id == download_id:
                if (
                    downloaded < e.downloaded_size
                    and e.status == "downloading"
                    and e.total_size > 0
                    and total == e.total_size
                    and (e.downloaded_size - downloaded < 1024 * 1024)
                    and downloaded > 0
                ):
                    return
                if e.status in ("completed", "seeding"):
                    if downloaded > 0:
                        e.downloaded_size = max(downloaded, e.downloaded_size)
                    elif e.total_size > 0:
                        e.downloaded_size = e.total_size
                else:
                    e.downloaded_size = downloaded
                if total > 0:
                    e.total_size = total
                e.speed = speed
                e.eta_seconds = eta
                e.seeds = to_int(seeds)
                e.peers = to_int(peers)
                if total_seeds > 0:
                    e.total_seeds = to_int(total_seeds)
                elif e.metadata and "total_seeds" in e.metadata:
                    e.total_seeds = to_int(e.metadata["total_seeds"])
                if total_peers > 0:
                    e.total_peers = to_int(total_peers)
                elif e.metadata and "total_peers" in e.metadata:
                    e.total_peers = to_int(e.metadata["total_peers"])
                e.upload_speed = upload_speed
                break

        row = self._id_to_row.get(download_id)
        if row is None:
            return

        # Emit change for relevant columns
        left = self.index(row, Col.SIZE)
        right = self.index(row, Col.SEEDS_PEERS)
        self.dataChanged.emit(left, right, [Qt.ItemDataRole.DisplayRole])

    def update_status(self, download_id: str, status: str,
                      error_msg: str = ""):
        entry_all: Optional[DownloadEntry] = None
        for e in self._all_entries:
            if e.id == download_id:
                e.status = status
                e.error_message = error_msg
                if status in ("paused", "completed", "error", "stopped"):
                    e.speed = 0
                    e.eta_seconds = 0
                entry_all = e
                break

        if self._segregated_view:
            self._reapply_filter()
            return

        row = self._id_to_row.get(download_id)
        matches = entry_all is not None and self._matches_filter(entry_all)

        if row is not None and not matches:
            self.beginRemoveRows(QModelIndex(), row, row)
            self._entries.pop(row)
            self._rebuild_index()
            self.endRemoveRows()
            if len(self._entries) > 0:
                self.dataChanged.emit(
                    self.index(0, Col.QUEUE),
                    self.index(len(self._entries) - 1, Col.QUEUE),
                    [Qt.ItemDataRole.DisplayRole],
                )
            return

        if row is None and matches and entry_all is not None:
            insert_row = self._find_insert_row(entry_all)
            self.beginInsertRows(QModelIndex(), insert_row, insert_row)
            self._entries.insert(insert_row, entry_all)
            self._rebuild_index()
            self.endInsertRows()
            if len(self._entries) > 0:
                self.dataChanged.emit(
                    self.index(0, Col.QUEUE),
                    self.index(len(self._entries) - 1, Col.QUEUE),
                    [Qt.ItemDataRole.DisplayRole],
                )
            return

        if row is None:
            return

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
        if len(self._entries) > 1:
            self.dataChanged.emit(
                self.index(0, Col.QUEUE),
                self.index(len(self._entries) - 1, Col.QUEUE),
                [Qt.ItemDataRole.DisplayRole],
            )

    def update_filename(self, download_id: str, filename: str):
        """Update filename when resolved from server headers or metadata."""
        for e in self._all_entries:
            if e.id == download_id:
                e.filename = filename
                if e.save_path:
                    e.file_path = str(Path(e.save_path) / filename)
                break

        row = self._id_to_row.get(download_id)
        if row is None:
            return
        entry = self._entries[row]
        entry.filename = filename
        if entry.save_path:
            entry.file_path = str(Path(entry.save_path) / filename)

        left = self.index(row, Col.NAME)
        right = self.index(row, Col.COUNT - 1)
        self.dataChanged.emit(
            left, right,
            [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole],
        )

    def rename_entry(self, download_id: str, filename: str, file_path: str = ""):
        """Update entry filename and file_path after user rename."""
        for e in self._all_entries:
            if e.id == download_id:
                if not e.metadata:
                    e.metadata = {}
                if not e.metadata.get("original_name"):
                    e.metadata["original_name"] = self.get_original_name(e)
                e.filename = filename
                if file_path:
                    e.file_path = file_path
                elif e.save_path:
                    e.file_path = str(Path(e.save_path) / filename)
                elif e.file_path:
                    e.file_path = str(Path(e.file_path).parent / filename)
                break

        row = self._id_to_row.get(download_id)
        if row is None:
            return
        entry = self._entries[row]
        if not entry.metadata:
            entry.metadata = {}
        if not entry.metadata.get("original_name"):
            entry.metadata["original_name"] = self.get_original_name(entry)
        entry.filename = filename
        if file_path:
            entry.file_path = file_path
        elif entry.save_path:
            entry.file_path = str(Path(entry.save_path) / filename)
        elif entry.file_path:
            entry.file_path = str(Path(entry.file_path).parent / filename)

        left = self.index(row, 0)
        right = self.index(row, Col.COUNT - 1)
        self.dataChanged.emit(left, right)

    def refresh_entry(self, download_id: str, entry: DownloadEntry):
        """Full refresh of an entry (e.g. after move or recheck)."""
        if entry.download_type == "torrent" and entry.metadata:
            if not entry.seeds and "seeds" in entry.metadata:
                entry.seeds = to_int(entry.metadata.get("seeds", 0))
            if not entry.peers and "peers" in entry.metadata:
                entry.peers = to_int(entry.metadata.get("peers", 0))
            if not getattr(entry, "total_seeds", 0) and "total_seeds" in entry.metadata:
                entry.total_seeds = to_int(entry.metadata.get("total_seeds", 0))
            if not getattr(entry, "total_peers", 0) and "total_peers" in entry.metadata:
                entry.total_peers = to_int(entry.metadata.get("total_peers", 0))
        entry.seeds = to_int(entry.seeds)
        entry.peers = to_int(entry.peers)
        entry.total_seeds = to_int(getattr(entry, "total_seeds", 0))
        entry.total_peers = to_int(getattr(entry, "total_peers", 0))

        for i, e in enumerate(self._all_entries):
            if e.id == download_id:
                self._all_entries[i] = entry
                break
        else:
            self._all_entries.append(entry)

        if self._segregated_view:
            self._reapply_filter()
            return

        row = self._id_to_row.get(download_id)
        matches = self._matches_filter(entry)

        if row is not None and not matches:
            self.beginRemoveRows(QModelIndex(), row, row)
            self._entries.pop(row)
            self._rebuild_index()
            self.endRemoveRows()
            return

        if row is None and matches:
            insert_row = self._find_insert_row(entry)
            self.beginInsertRows(QModelIndex(), insert_row, insert_row)
            self._entries.insert(insert_row, entry)
            self._rebuild_index()
            self.endInsertRows()
            return

        if row is not None:
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

        if getattr(entry, "is_section_header", False):
            if role == Qt.ItemDataRole.DisplayRole:
                if col == Col.QUEUE:
                    arrow = "▶" if entry.section_collapsed else "▼"
                    return f"  {arrow}   {entry.section_title.upper()} ({entry.section_count})"
                return ""
            if role == Qt.ItemDataRole.BackgroundRole:
                return QColor("#1e2330")
            if role == Qt.ItemDataRole.ForegroundRole:
                if entry.section_id == SECTION_ACTIVE:
                    return QColor(Colors.ACCENT)
                elif entry.section_id == SECTION_SEEDING:
                    return QColor(Colors.PURPLE)
                else:
                    return QColor("#8fa0b5")
            if role == Qt.ItemDataRole.FontRole:
                return QFont("Segoe UI", 10, QFont.Weight.Bold)
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            if role == Qt.ItemDataRole.ToolTipRole:
                action = "expand" if entry.section_collapsed else "collapse"
                return f"Click to {action} {entry.section_title} section"
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == Col.QUEUE:
                return int(Qt.AlignmentFlag.AlignCenter)

        if role == Qt.ItemDataRole.DecorationRole:
            if col == Col.NAME:
                if self.is_tor_active_for(entry):
                    return _get_icon("🧅")
                if entry.download_type == "torrent":
                    return _get_icon("🧲")
            if col in (Col.NAME, Col.FILE_NAME):
                if col == Col.FILE_NAME and entry.file_path and os.path.isdir(entry.file_path):
                    return _get_icon("📁")
                name_for_icon = self.get_actual_name(entry) if col == Col.FILE_NAME else self.get_original_name(entry)
                ext = Path(name_for_icon).suffix.lower()
                if ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".7zip"):
                    return _get_icon("📦")
                if ext in (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv"):
                    return _get_icon("🎬")
                if ext in (".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac"):
                    return _get_icon("🎵")
                if ext in (".iso", ".img", ".dmg", ".vhd"):
                    return _get_icon("💿")
                if ext in (".exe", ".msi", ".apk", ".deb", ".rpm", ".bat", ".cmd"):
                    return _get_icon("⚙️")
                if ext in (".pdf", ".doc", ".docx", ".epub", ".txt", ".odt"):
                    return _get_icon("📄")
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"):
                    return _get_icon("🖼️")
                if col == Col.FILE_NAME:
                    return _get_icon("📄")
                return _get_icon("🌐")

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_data(entry, col)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == Col.STATUS:
                if self.is_tor_active_for(entry):
                    return QColor(Colors.PURPLE)
                return _STATUS_COLORS.get(entry.status, QColor(Colors.TEXT))
            if col == Col.SOURCE_DOMAIN:
                return QColor(Colors.CYAN)

        if role == Qt.ItemDataRole.ToolTipRole:
            is_tor = self.is_tor_active_for(entry)
            tor_note = ""
            if is_tor and self._tor_config:
                tor_note = f"🧅 Active Tor Route: Routed via SOCKS5 proxy ({self._tor_config.socks5_url})"

            if col == Col.NAME:
                type_tag = f"[{entry.download_type.upper()}] " if entry.download_type else ""
                base = f"{type_tag}{self.get_original_name(entry)}"
                return f"{tor_note}\n{base}".strip() if tor_note else base
            if col == Col.FILE_NAME:
                return normalize_path(entry.file_path) if entry.file_path else self.get_actual_name(entry)
            if col == Col.STATUS:
                if entry.error_message:
                    return f"{tor_note}\n{entry.error_message}".strip() if tor_note else entry.error_message
                if is_tor and self._tor_config:
                    return f"Active Tor Transfer: Routed via SOCKS5 proxy ({self._tor_config.socks5_url})"

        if role == Qt.ItemDataRole.UserRole:
            if col == Col.SOURCE_DOMAIN:
                return extract_source_domain(entry.url)

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if 0 <= index.row() < len(self._entries) and getattr(self._entries[index.row()], "is_section_header", False):
            return Qt.ItemFlag.ItemIsEnabled
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )

    # -- display helpers -----------------------------------------------------

    def _display_data(self, entry: DownloadEntry, col: int) -> Any:
        if col == Col.QUEUE:
            if entry.status not in ACTIVE_QUEUE_STATUSES:
                return ""
            row = self._id_to_row.get(entry.id)
            if row is None:
                return ""
            count = 0
            for i in range(row + 1):
                if self._entries[i].status in ACTIVE_QUEUE_STATUSES:
                    count += 1
            return str(count)

        if col == Col.NAME:
            raw_name = self.get_original_name(entry)
            if self.is_tor_active_for(entry):
                return f"🧅 {raw_name}"
            return raw_name

        if col == Col.SOURCE_DOMAIN:
            return extract_source_domain(entry.url)

        if col == Col.SIZE:
            if entry.total_size > 0:
                return humanize.naturalsize(entry.total_size, binary=True)
            return "—"

        if col == Col.PROGRESS:
            # Return dict for ProgressBarDelegate
            prog = 100.0 if entry.status in ("completed", "seeding") else entry.progress
            return {
                "progress": prog,
                "status": entry.status,
            }

        if col == Col.STATUS:
            if entry.status == "threat_detected":
                return "Threat Detected ⚠"
            if entry.status == "scanning":
                return "Scanning 🛡️"
            if entry.status == "fetching_metadata":
                return "Fetching Metadata"
            if entry.status == "file_not_found":
                return "File Not Found ⚠"
            if entry.status == "stalled":
                return "Stalled"
            if entry.status == "stopped":
                return "Stopped"
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

        if col == Col.SEEDS_PEERS:
            if entry.download_type == "torrent":
                seeds = self._to_int(entry.seeds)
                peers = self._to_int(entry.peers)
                ts = self._to_int(getattr(entry, "total_seeds", 0) or (entry.metadata.get("total_seeds", 0) if entry.metadata else 0))
                tp = self._to_int(getattr(entry, "total_peers", 0) or (entry.metadata.get("total_peers", 0) if entry.metadata else 0))
                s_str = f"{seeds} ({ts})" if ts > seeds else f"{seeds}"
                p_str = f"{peers} ({tp})" if tp > peers else f"{peers}"
                return f"S:{s_str}  P:{p_str}"
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
            return normalize_path(entry.save_path) or "—"

        if col == Col.FILE_NAME:
            return self.get_actual_name(entry)

        return None

    @staticmethod
    def get_original_name(entry: DownloadEntry) -> str:
        """Resolve the original task / download title (before any rename)."""
        if entry.metadata:
            orig = entry.metadata.get("original_name")
            if orig and str(orig).strip():
                return str(orig).strip()
            tor_name = entry.metadata.get("torrent_name")
            if tor_name and str(tor_name).strip():
                return str(tor_name).strip()

        # Check magnet URI dn parameter
        if entry.url and "magnet:" in entry.url:
            try:
                query_str = entry.url.split("?", 1)[1] if "?" in entry.url else ""
                if query_str:
                    qs = parse_qs(query_str)
                    dns = qs.get("dn")
                    if dns and dns[0] and dns[0].strip():
                        return unquote_plus(dns[0]).strip()
            except Exception:
                pass

        # If entry was explicitly renamed and original_name is not stored, try extracting original from URL
        if entry.url and entry.url.startswith(("http://", "https://", "ftp://")):
            if entry.metadata and entry.metadata.get("explicit_filename"):
                try:
                    parsed = urlparse(entry.url)
                    if parsed.path:
                        base = os.path.basename(unquote(parsed.path))
                        if base and "." in base:
                            return base
                except Exception:
                    pass

        if entry.filename:
            return entry.filename

        return entry.url[:60] if entry.url else "—"

    @staticmethod
    def get_actual_name(entry: DownloadEntry) -> str:
        """Resolve the actual target file or root folder name on disk."""
        if entry.file_path:
            norm = normalize_path(entry.file_path)
            base = os.path.basename(norm)
            if base:
                return base
        if entry.filename:
            return entry.filename
        if entry.metadata and "files" in entry.metadata:
            files = entry.metadata["files"]
            if isinstance(files, list) and files:
                f0 = files[0]
                p = f0.get("path") if isinstance(f0, dict) else str(f0)
                if p:
                    norm_p = normalize_path(p)
                    parts = norm_p.split("/")
                    if len(parts) > 1 and parts[0]:
                        return parts[0]
                    elif parts[0]:
                        return parts[0]
        return "—"

    # -- index management ----------------------------------------------------

    def _rebuild_index(self):
        self._id_to_row = {
            e.id: i for i, e in enumerate(self._entries)
            if not getattr(e, "is_section_header", False)
        }
