"""QAbstractTableModel for the download list view."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional, Set

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
from my_idm.database import (
    ALL_QUEUES,
    DEFAULT_QUEUE_ID,
    DEFAULT_QUEUE_NAME,
    DownloadEntry,
)
from my_idm.styles import Colors
from my_idm.utils import create_emoji_icon, extract_source_domain, normalize_path, to_int
from my_idm import fonts

_ICON_CACHE: dict[str, Any] = {}

#: Custom item role carrying a row's queue swatch colour, for the Queue column's delegate.
#: A dedicated role rather than ``UserRole``, which is already column-specific (it answers the
#: source *domain* for that column), and rather than decoding a colour back out of the cell
#: text, which would tie the delegate to a presentation detail.
QUEUE_COLOR_ROLE = Qt.ItemDataRole.UserRole + 1

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
    # Appended at the end so persisted column indices (column_widths,
    # header_state, sort_column in ui_state) keep pointing at the same columns.
    LAST_SEEDED = 14
    SOURCE = 15
    SEEDING_STARTED_AT = 16
    QUEUE_NAME = 17

    DATE_COLUMNS = (ADDED, LAST_TRIED, COMPLETED)

    HEADERS = [
        "#", "Name", "Source Domain", "Size", "Progress", "Status", "Speed", "ETA",
        "Seeds / Peers", "Added", "Last Tried", "Completed",
        "Save Path", "File / Folder Name", "Last Seeded", "Source", "Seeding Started At",
        "Queue",
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
    "downloading",
    "queued",
    "paused",
}


def _format_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    return f"{humanize.naturalsize(bps, binary=True)}/s"


def is_youtube_entry(entry: DownloadEntry) -> bool:
    """True when the entry came from a YouTube / yt-dlp download."""
    if entry is None or not entry.metadata:
        return False
    return str(entry.metadata.get("source_type", "")).startswith("youtube")


# Order matters: Edge's User-Agent also advertises "Chrome", so its token
# ("Edg/", or "Edge/" for the legacy EdgeHTML build) must be tested first.
_BROWSER_UA_SIGNATURES = (
    ("Edge", "edg/"),
    ("Edge", "edge/"),
    ("Firefox", "firefox"),
    ("Chrome", "chrome"),
)

ANIMEPAHE_SOURCE = "AnimePahe"
YOUTUBE_SOURCE = "YouTube"


def browser_from_user_agent(user_agent: str) -> str:
    """Return Chrome, Firefox, or Edge from a User-Agent string, else empty."""
    ua = (user_agent or "").lower()
    if not ua:
        return ""
    for label, token in _BROWSER_UA_SIGNATURES:
        if token in ua:
            return label
    return ""


def resolve_download_source(entry: DownloadEntry) -> str:
    """Return where a download came from: Chrome, Firefox, Edge, AnimePahe, YouTube.

    Returns an empty string for manually added downloads and for legacy rows that
    carry no provenance metadata, which is what the Source column renders as blank.

    Derived from ``metadata_json`` rather than stored in its own column so existing
    rows are classified immediately with no migration and no backfill.
    """
    if entry is None or not entry.metadata:
        return ""
    meta = entry.metadata

    if str(meta.get("source_type", "")).startswith("youtube"):
        return YOUTUBE_SOURCE

    added_by = str(meta.get("added_by", "")).lower()
    if "animepahe" in added_by:
        return ANIMEPAHE_SOURCE

    if meta.get("source") == "browser_extension":
        return browser_from_user_agent(str(meta.get("user_agent", "")))

    return ""


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

# Size buckets for the Col.SIZE filter popup. Ordered smallest to largest, and
# keyed by a stable id (the popup persists a selection set), with the label used
# for display. Thresholds are binary (1024-based) to match how the Size column
# renders sizes. Ranges are half-open: [low, high), so a bucket's lower bound is
# inclusive and its upper bound exclusive.
_MB = 1024 * 1024
_GB = 1024 * _MB

SIZE_FILTER_BUCKETS: list[tuple[str, str, int, float]] = [
    ("lt_10mb", "<10MB", 0, 10 * _MB),
    ("10mb_100mb", "10-100MB", 10 * _MB, 100 * _MB),
    ("100mb_1gb", "100MB-1GB", 100 * _MB, 1 * _GB),
    ("1gb_5gb", "1-5GB", 1 * _GB, 5 * _GB),
    ("5gb_10gb", "5-10GB", 5 * _GB, 10 * _GB),
    ("10gb_50gb", "10-50GB", 10 * _GB, 50 * _GB),
    ("gt_50gb", ">50GB", 50 * _GB, float("inf")),
]

SIZE_FILTER_LABELS: dict[str, str] = {k: label for k, label, _lo, _hi in SIZE_FILTER_BUCKETS}


def size_bucket_for(size_bytes: Any) -> str:
    """Return the size-bucket id for *size_bytes*.

    A zero or unknown size falls into the smallest bucket, which is the literal
    reading of "<10MB" and keeps such rows selectable rather than invisible.
    """
    size = to_int(size_bytes, 0)
    if size < 0:
        size = 0
    for key, _label, low, high in SIZE_FILTER_BUCKETS:
        if low <= size < high:
            return key
    return SIZE_FILTER_BUCKETS[-1][0]

SECTION_ACTIVE = "active"
SECTION_SEEDING = "seeding"
SECTION_INACTIVE = "inactive"

SECTION_DATE_TODAY = "date_today"
SECTION_DATE_YESTERDAY = "date_yesterday"
SECTION_DATE_LAST_7_DAYS = "date_last_7_days"
SECTION_DATE_LAST_30_DAYS = "date_last_30_days"
SECTION_DATE_OLDER = "date_older"

# Backward-compatibility aliases
SECTION_DATE_THIS_WEEK = SECTION_DATE_LAST_7_DAYS
SECTION_DATE_THIS_MONTH = SECTION_DATE_LAST_30_DAYS

ACTIVE_SECTION_STATUSES = {
    "fetching_metadata", "queued", "downloading", "paused", "stalled", "error",
    "checking", "scanning", "threat_detected"
}
SEEDING_SECTION_STATUSES = {"seeding"}
INACTIVE_SECTION_STATUSES = {"completed", "stopped", "file_not_found", "suspended"}

# -- File-type segregation -------------------------------------------------
# A third segregated mode, grouping by what the payload *is* rather than when it was added
# or whether it is still running. Users accumulate mixed libraries and the only way to find
# "the PDFs" today is the Name column plus manual sorting.
SECTION_TYPE_VIDEO = "type_video"
SECTION_TYPE_AUDIO = "type_audio"
SECTION_TYPE_ARCHIVE = "type_archive"
SECTION_TYPE_DOCUMENTS = "type_documents"
SECTION_TYPE_PHOTO = "type_photo"
SECTION_TYPE_GENERAL = "type_general"

#: Extension -> category. Lower-case, no leading dot. Ordered most-specific first: a
#: compound extension like ``tar.gz`` is matched by its **last** component below, so only
#: the tail needs listing here.
TYPE_CATEGORY_EXTENSIONS: dict[str, str] = {
    # Video
    "mp4": SECTION_TYPE_VIDEO, "mkv": SECTION_TYPE_VIDEO, "avi": SECTION_TYPE_VIDEO,
    "mov": SECTION_TYPE_VIDEO, "wmv": SECTION_TYPE_VIDEO, "flv": SECTION_TYPE_VIDEO,
    "webm": SECTION_TYPE_VIDEO, "m4v": SECTION_TYPE_VIDEO, "mpg": SECTION_TYPE_VIDEO,
    "mpeg": SECTION_TYPE_VIDEO, "ts": SECTION_TYPE_VIDEO, "m2ts": SECTION_TYPE_VIDEO,
    "3gp": SECTION_TYPE_VIDEO, "vob": SECTION_TYPE_VIDEO, "rmvb": SECTION_TYPE_VIDEO,
    # Audio
    "mp3": SECTION_TYPE_AUDIO, "flac": SECTION_TYPE_AUDIO, "aac": SECTION_TYPE_AUDIO,
    "ogg": SECTION_TYPE_AUDIO, "oga": SECTION_TYPE_AUDIO, "wav": SECTION_TYPE_AUDIO,
    "m4a": SECTION_TYPE_AUDIO, "wma": SECTION_TYPE_AUDIO, "opus": SECTION_TYPE_AUDIO,
    "aiff": SECTION_TYPE_AUDIO, "alac": SECTION_TYPE_AUDIO, "mid": SECTION_TYPE_AUDIO,
    # Archive
    "zip": SECTION_TYPE_ARCHIVE, "rar": SECTION_TYPE_ARCHIVE,
    "7z": SECTION_TYPE_ARCHIVE, "gz": SECTION_TYPE_ARCHIVE,
    "bz2": SECTION_TYPE_ARCHIVE, "xz": SECTION_TYPE_ARCHIVE,
    "zst": SECTION_TYPE_ARCHIVE, "tar": SECTION_TYPE_ARCHIVE,
    "cab": SECTION_TYPE_ARCHIVE, "arj": SECTION_TYPE_ARCHIVE, "lzh": SECTION_TYPE_ARCHIVE,
    "iso": SECTION_TYPE_ARCHIVE, "tgz": SECTION_TYPE_ARCHIVE,
    # Documents
    "pdf": SECTION_TYPE_DOCUMENTS, "epub": SECTION_TYPE_DOCUMENTS,
    "mobi": SECTION_TYPE_DOCUMENTS, "azw3": SECTION_TYPE_DOCUMENTS,
    "doc": SECTION_TYPE_DOCUMENTS, "docx": SECTION_TYPE_DOCUMENTS,
    "xls": SECTION_TYPE_DOCUMENTS, "xlsx": SECTION_TYPE_DOCUMENTS,
    "ppt": SECTION_TYPE_DOCUMENTS, "pptx": SECTION_TYPE_DOCUMENTS,
    "odt": SECTION_TYPE_DOCUMENTS, "ods": SECTION_TYPE_DOCUMENTS,
    "odp": SECTION_TYPE_DOCUMENTS, "rtf": SECTION_TYPE_DOCUMENTS,
    "txt": SECTION_TYPE_DOCUMENTS, "md": SECTION_TYPE_DOCUMENTS,
    "csv": SECTION_TYPE_DOCUMENTS, "json": SECTION_TYPE_DOCUMENTS,
    "xml": SECTION_TYPE_DOCUMENTS, "html": SECTION_TYPE_DOCUMENTS,
    "htm": SECTION_TYPE_DOCUMENTS, "chm": SECTION_TYPE_DOCUMENTS,
    # Photo
    "jpg": SECTION_TYPE_PHOTO, "jpeg": SECTION_TYPE_PHOTO, "png": SECTION_TYPE_PHOTO,
    "gif": SECTION_TYPE_PHOTO, "bmp": SECTION_TYPE_PHOTO, "webp": SECTION_TYPE_PHOTO,
    "tiff": SECTION_TYPE_PHOTO, "tif": SECTION_TYPE_PHOTO, "svg": SECTION_TYPE_PHOTO,
    "heic": SECTION_TYPE_PHOTO, "heif": SECTION_TYPE_PHOTO, "avif": SECTION_TYPE_PHOTO,
    "raw": SECTION_TYPE_PHOTO, "cr2": SECTION_TYPE_PHOTO, "nef": SECTION_TYPE_PHOTO,
    "arw": SECTION_TYPE_PHOTO, "dng": SECTION_TYPE_PHOTO, "psd": SECTION_TYPE_PHOTO,
    "ico": SECTION_TYPE_PHOTO,
}

#: Display order of the file-type sections in the table.
TYPE_SECTION_DEFS = [
    (SECTION_TYPE_VIDEO, "Video", "__section_type_video__"),
    (SECTION_TYPE_AUDIO, "Audio", "__section_type_audio__"),
    (SECTION_TYPE_ARCHIVE, "Archives", "__section_type_archive__"),
    (SECTION_TYPE_DOCUMENTS, "Documents", "__section_type_documents__"),
    (SECTION_TYPE_PHOTO, "Photos", "__section_type_photo__"),
    (SECTION_TYPE_GENERAL, "General", "__section_type_general__"),
]

#: The segregated modes the View menu offers, in menu order.
SEGREGATED_MODES = ("status", "date", "type")
DEFAULT_SEGREGATED_MODE = "status"

#: Menu / UI labels for the modes, kept beside the modes so the View menu and the
#: Preferences tab cannot drift apart.
SEGREGATED_MODE_LABELS = {
    "status": "Status (Active / Seeding / Inactive)",
    "date": "Date (Today / Yesterday / Last 7 Days / Last 30 Days / Older)",
    "type": "File Type (Video / Audio / Archives / Documents / Photos / General)",
}


def split_extension(name: str) -> tuple[str, str]:
    """Split *name* into (stem, extension-with-dot), lower-cased and dot-prefixed.

    Returns ``("", "")`` for a blank name. Mirrors ``utils.split_extension`` but is kept
    local so ``download_model`` stays importable without ``utils`` (and therefore without
    Qt-free test collection pulling in the whole app).
    """
    if not name:
        return "", ""
    base = name.replace("\\", "/").rstrip("/").split("/")[-1]
    idx = base.rfind(".")
    # A leading dot is a hidden file, not an extension (".gitignore" has none).
    if idx <= 0 or idx == len(base) - 1:
        return base, ""
    return base[:idx], base[idx:].lower()


def get_entry_type_category(entry: DownloadEntry) -> str:
    """Classify a download into Video / Audio / Archive / Documents / Photo / General.

    The extension is taken from ``filename``, falling back to the leaf of ``file_path`` so
    a torrent whose root folder carries the extension still lands in the right section.
    Anything unrecognised - including a download with no name yet - is ``General``, which
    is the catch-all section and therefore never empty-by-accident.
    """
    candidates = []
    filename = (getattr(entry, "filename", "") or "").strip()
    if filename:
        candidates.append(filename)
    file_path = (getattr(entry, "file_path", "") or "").strip()
    if file_path:
        candidates.append(file_path.replace("\\", "/").rstrip("/").split("/")[-1])
    # An explicit user override wins, so a mis-detected item can be filed by hand.
    override = ""
    try:
        meta = entry.metadata
        if meta:
            override = str(meta.get("type_category") or "")
    except Exception:
        override = ""
    if override in {cat for cat, _t, _s in TYPE_SECTION_DEFS}:
        return override

    for candidate in candidates:
        _stem, ext = split_extension(candidate)
        if ext:
            category = TYPE_CATEGORY_EXTENSIONS.get(ext.lstrip("."))
            if category:
                return category
    return SECTION_TYPE_GENERAL

DATE_SECTION_DEFS = [
    (SECTION_DATE_TODAY, "Today", "__section_date_today__"),
    (SECTION_DATE_YESTERDAY, "Yesterday", "__section_date_yesterday__"),
    (SECTION_DATE_LAST_7_DAYS, "Last 7 Days", "__section_date_last_7_days__"),
    (SECTION_DATE_LAST_30_DAYS, "Last 30 Days", "__section_date_last_30_days__"),
    (SECTION_DATE_OLDER, "Older", "__section_date_older__"),
]


def get_entry_latest_timestamp(entry: DownloadEntry) -> Optional[datetime]:
    """Return the latest datetime among added_at, completed_at, and last_tried_at."""
    candidates: list[datetime] = []
    for raw in (entry.added_at, entry.completed_at, entry.last_tried_at):
        if not raw or not isinstance(raw, str):
            continue
        raw_s = raw.strip()
        if not raw_s:
            continue
        try:
            dt = datetime.fromisoformat(raw_s)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            else:
                dt = dt.astimezone()
            candidates.append(dt)
        except (ValueError, TypeError):
            continue
    return max(candidates) if candidates else None


def get_entry_date_category(entry: DownloadEntry, now_dt: Optional[datetime] = None) -> str:
    """Classify download entry into Today, Yesterday, Last 7 Days, Last 30 Days, or Older."""
    latest_dt = get_entry_latest_timestamp(entry)
    if latest_dt is None:
        return SECTION_DATE_OLDER

    if now_dt is None:
        now_dt = datetime.now().astimezone()

    today = now_dt.date()
    entry_date = latest_dt.date()
    diff_days = (today - entry_date).days

    if diff_days <= 0:
        return SECTION_DATE_TODAY
    elif diff_days == 1:
        return SECTION_DATE_YESTERDAY
    elif diff_days <= 7:
        return SECTION_DATE_LAST_7_DAYS
    elif diff_days <= 30:
        return SECTION_DATE_LAST_30_DAYS
    else:
        return SECTION_DATE_OLDER


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
        self._tor_available_provider = None
        self._search_query: str = ""
        self._status_filter: Optional[set[str]] = None
        self._type_filter: Optional[set[str]] = None
        self._size_filter: Optional[set[str]] = None
        self._segregated_view: bool = False
        self._segregated_mode: str = "status"
        self._collapsed_sections: set[str] = set()
        # Local calendar date the last "date" segregation was built against. The sections are
        # relative to today (Today / Yesterday / Last 7 Days), so the grouping silently goes
        # stale when the local day rolls over under a running app. None means "never built",
        # which refresh_date_grouping() treats as stale. Set only by the date branch of
        # _apply_sort, which is the single place that reads the clock for this purpose.
        self._segregation_date: Optional[date] = None
        # Which named queue the view is scoped to. ALL_QUEUES ("") means every queue, which is
        # the default and the startup state: history is the product, so downloads the user
        # already had must never go missing just because a queue is selected.
        self._queue_scope: str = ALL_QUEUES
        # queue_id -> display name, for Col.QUEUE_NAME. Injected rather than read from the
        # database: the model has no DB handle, and queue names are the one thing a row cannot
        # carry itself (unlike Source, which is derived from metadata_json).
        self._queue_names: dict[str, str] = {}
        # queue_id -> "#rrggbb", for the swatch in Col.QUEUE_NAME. Same injection reason as
        # _queue_names: the model has no database handle.
        self._queue_colors: dict[str, str] = {}
        # Queue ids ticked in the Queue column's header filter; None means no filter. Distinct
        # from _queue_scope, which is the toolbar's single-queue selection.
        self._queue_filter: Optional[Set[str]] = None

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

    def is_tor_routed_by_choice(self, entry: DownloadEntry) -> bool:
        """True when this individual download is flagged to route through Tor."""
        if entry is None:
            return False
        try:
            return bool(entry.metadata.get("route_through_tor", False))
        except Exception:
            return False

    def is_tor_active_for(self, entry: DownloadEntry) -> bool:
        """Return True if this download is actively transferring over Tor right now.

        Two independent ways a row can be on Tor: the global Tor switch routing
        its protocol, or the per-download flag. The flag is only honoured while a
        Tor proxy is actually reachable, so a stale flag does not light up the
        badge after Tor is stopped.
        """
        if entry is None:
            return False
        if self.is_tor_routed_by_choice(entry):
            if not self._tor_available():
                return False
            if entry.download_type == "http":
                return entry.status in ("downloading", "fetching_metadata", "stalled")
            if entry.download_type == "torrent":
                return entry.status in ("downloading", "seeding")
            return False

        if not self._tor_config or not self._tor_config.enabled:
            return False
        if entry.download_type == "http":
            return entry.status == "downloading" and self._tor_config.route_http
        if entry.download_type == "torrent":
            return entry.status in ("downloading", "seeding") and self._tor_config.route_torrent
        return False

    def set_tor_availability_provider(self, provider) -> None:
        """Inject a callable reporting whether a Tor proxy is live right now."""
        self._tor_available_provider = provider

    def tor_available(self) -> bool:
        if self._tor_available_provider is None:
            return False
        try:
            return bool(self._tor_available_provider())
        except Exception:
            return False

    def _tor_available(self) -> bool:
        return self.tor_available()

    @property
    def sort_column(self) -> int:
        return self._sort_column

    @property
    def sort_order(self) -> Qt.SortOrder:
        return self._sort_order

    # -- helpers & filtering ---------------------------------------------------

    _to_int = staticmethod(to_int)

    def _matches_search(self, entry: DownloadEntry) -> bool:
        """True when *entry* matches the quick-search query, or there is none."""
        query = self._search_query
        if not query:
            return True
        if query in (getattr(entry, "original_name", "") or "").lower():
            return True
        if query in (entry.filename or "").lower():
            return True
        if query in (entry.url or "").lower():
            return True
        domain = extract_source_domain(entry.url)
        return bool(domain) and query in domain.lower()

    def _in_queue_scope(self, entry: DownloadEntry) -> bool:
        """Whether *entry* belongs to the queue the view is scoped to.

        Its own method rather than a line inside ``_matches_filter`` because the three
        ``get_*_counts`` members below hand-roll their own filter chains over ``_all_entries``,
        and a scope applied in only one of them would make the header chip counts disagree with
        the rows they filter.
        """
        if not self._queue_scope:
            return True
        return (entry.queue_id or DEFAULT_QUEUE_ID) == self._queue_scope

    def _in_queue_filter(self, entry: DownloadEntry) -> bool:
        """Whether *entry* passes the Queue column's header filter.

        Distinct from ``_in_queue_scope``: the scope narrows the list to one queue from the
        toolbar, this picks any number of them from the column header, and both can be active
        at once.
        """
        if self._queue_filter is None:
            return True
        return (entry.queue_id or DEFAULT_QUEUE_ID) in self._queue_filter

    def _matches_filter(self, entry: DownloadEntry) -> bool:
        if not self._in_queue_scope(entry):
            return False
        if not self._in_queue_filter(entry):
            return False
        if not self._matches_search(entry):
            return False
        if self._type_filter is not None:
            dtype = entry.download_type or "http"
            if dtype not in self._type_filter:
                return False
        if self._size_filter is not None:
            if size_bucket_for(entry.total_size) not in self._size_filter:
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

    def _reapply_filter(self, now_dt: Optional[datetime] = None):
        self.beginResetModel()
        self._apply_sort(now_dt)
        self._rebuild_index()
        self.endResetModel()

    def set_segregated_view(self, enabled: bool, mode: Optional[str] = None):
        # An unknown mode is ignored rather than stored, so a stale persisted value from a
        # newer build (or a hand-edited ui_state) degrades to the default instead of
        # silently disabling segregation.
        if mode is not None and mode in SEGREGATED_MODES:
            self._segregated_mode = mode
        if self._segregated_mode not in SEGREGATED_MODES:
            self._segregated_mode = DEFAULT_SEGREGATED_MODE
        if self._segregated_view == enabled and mode is None:
            return
        self._segregated_view = enabled
        self._reapply_filter()

    def is_segregated_view(self) -> bool:
        return self._segregated_view

    def set_segregated_mode(self, mode: str):
        if mode not in SEGREGATED_MODES:
            mode = DEFAULT_SEGREGATED_MODE
        if self._segregated_mode != mode:
            self._segregated_mode = mode
            if self._segregated_view:
                self._reapply_filter()

    def segregated_mode(self) -> str:
        return self._segregated_mode

    def date_grouping_is_stale(self, now_dt: Optional[datetime] = None) -> bool:
        """True when the "date" sections were built against a different local day.

        Callers that must capture view state *before* triggering a rebuild (a model reset
        drops the selection) need to ask first and act second, so this is deliberately
        separate from refresh_date_grouping() rather than folded into its return value.
        """
        if not self._segregated_view or self._segregated_mode != "date":
            return False
        if now_dt is None:
            now_dt = datetime.now().astimezone()
        # None means the sections have never been built, which is as stale as a past day.
        return self._segregation_date != now_dt.date()

    def refresh_date_grouping(self, now_dt: Optional[datetime] = None) -> bool:
        """Rebuild the date sections if the local calendar day has rolled over.

        The Today / Yesterday / Last 7 Days sections are relative to the *current* day, so a
        long-lived window showing yesterday's grouping is just wrong: nothing else schedules a
        rebuild, and _apply_sort only reads the clock when something else already triggered one.
        A quiet app therefore kept "Today" full of yesterday's downloads all day. The caller is
        expected to poll this (MainWindow's 1 Hz details tick does) and to re-apply selection
        afterwards, because _reapply_filter resets the model and drops the view's selection.

        Returns True when a rebuild happened, so the caller can skip its usual work otherwise.
        Only the "date" mode is affected: status and file-type sections do not depend on today.
        """
        if not self.date_grouping_is_stale(now_dt):
            return False
        self._reapply_filter(now_dt)
        return True

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
        return (
            self._status_filter is not None
            or self._type_filter is not None
            or self._size_filter is not None
            or bool(self._search_query)
            or bool(self._queue_scope)
            or self._queue_filter is not None
        )

    def queue_scope(self) -> str:
        """The queue the view is scoped to, or ``""`` for all queues."""
        return self._queue_scope

    def set_queue_names(self, names: dict[str, str]):
        """Supply the queue_id -> display name mapping used by ``Col.QUEUE_NAME``.

        A rename or a deletion changes what several rows show at once, so the whole mapping is
        replaced rather than diffed. Rows whose queue is not in the mapping render as the
        default queue's name, which is what a blank ``queue_id`` means anyway.
        """
        if names == self._queue_names:
            return
        self._queue_names = dict(names)

    def set_queue_colors(self, colors: dict[str, str]):
        """Supply the queue_id -> ``#rrggbb`` mapping used by the Queue column's swatch."""
        if colors == self._queue_colors:
            return
        self._queue_colors = dict(colors)

    def queue_color_for(self, entry: DownloadEntry) -> str:
        """Swatch colour for *entry*'s queue, or "" when the queue has none."""
        return self._queue_colors.get(entry.queue_id or DEFAULT_QUEUE_ID, "")

    def queue_name_for(self, entry: DownloadEntry) -> str:
        """Display name of *entry*'s queue.

        Falls back to the default queue's name for an unknown id rather than showing a raw
        uuid: the write paths normalise, so this only happens if a row was repaired by hand.
        """
        return self._queue_names.get(
            entry.queue_id or DEFAULT_QUEUE_ID, DEFAULT_QUEUE_NAME
        )

    def set_queue_scope(self, queue_id: str):
        """Scope the view to one queue, or to every queue when *queue_id* is blank."""
        resolved = queue_id or ALL_QUEUES
        if resolved == self._queue_scope:
            return
        self._queue_scope = resolved
        self._reapply_filter()

    def search_query(self) -> str:
        return self._search_query

    def is_searching(self) -> bool:
        return bool(self._search_query)

    def set_search_query(self, query: str) -> None:
        """Filter rows by a free-text query (name, original name, URL, domain)."""
        normalized = (query or "").strip().lower()
        if normalized == self._search_query:
            return
        self._search_query = normalized
        self._reapply_filter()

    def is_status_filtered(self) -> bool:
        return self._status_filter is not None

    def is_type_filtered(self) -> bool:
        return self._type_filter is not None

    def is_size_filtered(self) -> bool:
        return self._size_filter is not None

    def size_filter(self) -> Optional[set[str]]:
        return self._size_filter

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

    def set_size_filter(self, allowed_buckets: Optional[set[str]]):
        if allowed_buckets is not None and len(allowed_buckets) >= len(SIZE_FILTER_BUCKETS):
            allowed_buckets = None
        if self._size_filter == allowed_buckets:
            return
        self._size_filter = set(allowed_buckets) if allowed_buckets is not None else None
        self._reapply_filter()

    def set_queue_filter(self, allowed_ids: Optional[set[str]]):
        """Restrict the view to downloads in the named queues.

        Keys are queue **ids**, not names: a rename would otherwise leave the filter holding a
        label that matches nothing, and the view would silently come up empty. The popup shows
        the name; the filter stores the id.
        """
        if allowed_ids is not None and len(allowed_ids) >= len(self._queue_names):
            # Everything ticked is the same as nothing ticked, which is how Select All reads.
            allowed_ids = None
        if self._queue_filter == allowed_ids:
            return
        self._queue_filter = set(allowed_ids) if allowed_ids is not None else None
        self._reapply_filter()

    def queue_filter(self) -> Optional[set[str]]:
        """The queue ids currently ticked, or ``None`` for no filter."""
        return self._queue_filter

    def is_queue_filtered(self) -> bool:
        return self._queue_filter is not None

    def queue_filter_items(self) -> list[tuple[str, str]]:
        """``(queue_id, queue_name)`` pairs for the filter popup, default queue first."""
        return list(self._queue_names.items())

    def clear_filters(self):
        """Clear the header filters. The search box is cleared separately."""
        if (
            self._status_filter is None
            and self._type_filter is None
            and self._size_filter is None
            and self._queue_filter is None
        ):
            return
        self._status_filter = None
        self._type_filter = None
        self._size_filter = None
        self._queue_filter = None
        self._reapply_filter()

    def get_queue_counts(self) -> dict[str, int]:
        """Rows per queue id, honouring the other active filters.

        The Queue filter's own selection is deliberately *not* applied here: a filter popup
        that counted only the ticked queues would show every other queue as 0 and read as if
        they were empty.
        """
        counts = {queue_id: 0 for queue_id in self._queue_names}
        for entry in self._all_entries:
            if getattr(entry, "is_section_header", False):
                continue
            if not self._in_queue_scope(entry):
                continue
            if not self._matches_search(entry):
                continue
            if self._type_filter is not None and (
                entry.download_type or "http"
            ) not in self._type_filter:
                continue
            if self._status_filter is not None and not any(
                entry.status in STATUS_FILTER_GROUPS.get(group, {group})
                for group in self._status_filter
            ):
                continue
            key = entry.queue_id or DEFAULT_QUEUE_ID
            counts[key] = counts.get(key, 0) + 1
        return counts

    def get_size_counts(self) -> dict[str, int]:
        """Rows per size bucket, honouring the other active filters."""
        counts = {key: 0 for key, _label, _lo, _hi in SIZE_FILTER_BUCKETS}
        for e in self._all_entries:
            if getattr(e, "is_section_header", False):
                continue
            if not self._in_queue_scope(e):
                continue
            if self._type_filter is not None and (e.download_type or "http") not in self._type_filter:
                continue
            if self._status_filter is not None:
                if not any(
                    e.status in STATUS_FILTER_GROUPS.get(g, {g}) for g in self._status_filter
                ):
                    continue
            counts[size_bucket_for(e.total_size)] += 1
        return counts

    def total_unfiltered_count(self) -> int:
        return len(self._all_entries)

    def visible_download_count(self) -> int:
        return sum(1 for e in self._entries if not getattr(e, "is_section_header", False))

    def get_status_counts(self) -> dict[str, int]:
        counts = {k: 0 for k in STATUS_FILTER_GROUPS}
        for e in self._all_entries:
            if not self._in_queue_scope(e):
                continue
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
            if not self._in_queue_scope(e):
                continue
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

    def _apply_sort(self, now_dt: Optional[datetime] = None):
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

        buckets: dict[str, list[DownloadEntry]]
        if self._segregated_mode == "date":
            # Segregated view: group by Today, Yesterday, Last 7 Days, Last 30 Days, Older.
            # Buckets and section ids both come from DATE_SECTION_DEFS so the header rows and
            # the classifier cannot drift apart; the literals used to be spelled twice.
            buckets = {
                sec_id: [] for sec_id, _t, _s in DATE_SECTION_DEFS
            }

            if now_dt is None:
                now_dt = datetime.now().astimezone()
            # Remember the day this snapshot was built against so refresh_date_grouping() can
            # tell a stale grouping from a current one without diffing the sections.
            self._segregation_date = now_dt.date()
            for e in filtered:
                # get_entry_date_category only ever returns the five ids above (the old
                # "date_this_week"/"date_this_month" spellings are aliased onto the canonical
                # ones at module level), but an unknown value must not silently drop a row.
                buckets.setdefault(
                    get_entry_date_category(e, now_dt), buckets[SECTION_DATE_OLDER]
                ).append(e)

            groups = [
                (sec_id, title, buckets[sec_id], hdr_id)
                for sec_id, title, hdr_id in DATE_SECTION_DEFS
            ]
        else:
            # Segregated view: group by Active, Seeding, Inactive, or by file type.
            if self._segregated_mode == "type":
                buckets = {
                    cat: [] for cat, _t, _s in TYPE_SECTION_DEFS
                }
                for e in filtered:
                    buckets[get_entry_type_category(e)].append(e)
                groups = [
                    (cat, title, buckets[cat], sentinel)
                    for cat, title, sentinel in TYPE_SECTION_DEFS
                ]
            else:
                active_entries = [e for e in filtered if e.status in ACTIVE_SECTION_STATUSES]
                seeding_entries = [e for e in filtered if e.status in SEEDING_SECTION_STATUSES]
                inactive_entries = [e for e in filtered if e.status in INACTIVE_SECTION_STATUSES]

                groups = [
                    (SECTION_ACTIVE, "Active", active_entries, "__section_active__"),
                    (SECTION_SEEDING, "Seeding", seeding_entries, "__section_seeding__"),
                    (SECTION_INACTIVE, "Inactive", inactive_entries, "__section_inactive__"),
                ]

        if self._sort_column in (Col.ADDED, Col.QUEUE) or self._sort_column is None:
            for sec_id, _, group_entries, _ in groups:
                if self._sort_column == Col.ADDED and ascending:
                    group_entries.sort(
                        key=lambda e: self._entry_sort_key(e, self._sort_column, ascending),
                        reverse=False,
                    )
                else:
                    active_group = [e for e in group_entries if e.status in ACTIVE_QUEUE_STATUSES]
                    inactive_group = [e for e in group_entries if e.status not in ACTIVE_QUEUE_STATUSES]
                    active_group.sort(
                        key=lambda e: (e.queue_order if e.queue_order > 0 else 999999, e.added_at or "", e.id),
                        reverse=(self._sort_column == Col.QUEUE and not ascending),
                    )
                    if self._sort_column is not None:
                        inactive_group.sort(
                            key=lambda e: self._entry_sort_key(e, self._sort_column, ascending),
                            reverse=reverse,
                        )
                    group_entries[:] = active_group + inactive_group
        elif self._sort_column is not None:
            for sec_id, _, group_entries, _ in groups:
                group_entries.sort(
                    key=lambda e: self._entry_sort_key(e, self._sort_column, ascending),
                    reverse=reverse,
                )

        entries: list[DownloadEntry] = []
        for sec_id, title, group_entries, hdr_id in groups:
            hdr = DownloadEntry(
                id=hdr_id,
                is_section_header=True,
                status="section_header",
                section_id=sec_id,
                section_title=title,
                section_count=len(group_entries),
                section_collapsed=(sec_id in self._collapsed_sections),
            )
            entries.append(hdr)
            if sec_id not in self._collapsed_sections:
                entries.extend(group_entries)

        self._entries = entries

    def _entry_sort_key(self, entry: DownloadEntry, col: int, ascending: bool) -> Any:
        if col == Col.QUEUE:
            is_active = entry.status in ACTIVE_QUEUE_STATUSES
            val = entry.queue_order if entry.queue_order > 0 else 999999
            if ascending:
                return (0 if is_active else 1, val, entry.added_at or "", entry.id)
            else:
                return (1 if is_active else 0, val, entry.added_at or "", entry.id)

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

        if col == Col.LAST_SEEDED:
            # Same (has_value, value) shape as the other date columns so the
            # key stays type-compatible and empty timestamps sort last.
            has_time = bool(entry.last_seeded_at)
            if ascending:
                return (0, entry.last_seeded_at) if has_time else (1, "")
            else:
                return (1, entry.last_seeded_at) if has_time else (0, "")

        if col == Col.SOURCE:
            return resolve_download_source(entry).lower()

        if col == Col.SEEDING_STARTED_AT:
            has_time = bool(entry.seeding_started_at)
            if ascending:
                return (0, entry.seeding_started_at) if has_time else (1, "")
            else:
                return (1, entry.seeding_started_at) if has_time else (0, "")

        if col == Col.QUEUE_NAME:
            # Plain case-insensitive name compare, same shape as Col.SOURCE. Ties fall back to
            # the shared (queue_order, added_at) ordering in _apply_sort.
            return self.queue_name_for(entry).lower()

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

    def get_section_header(self, row: int) -> Optional[DownloadEntry]:
        """The section-header row at *row*, or None if that row is a real download.

        The counterpart to :meth:`get_entry`, which deliberately hides section rows. Callers
        that need to read a section's title or count - the delegate does it for display,
        and the Views preferences tab does it for the mode list - need this rather than
        reaching into ``_entries``.
        """
        if 0 <= row < len(self._entries):
            e = self._entries[row]
            return e if getattr(e, "is_section_header", False) else None
        return None

    def get_entry_by_id(self, download_id: str) -> Optional[DownloadEntry]:
        row = self._id_to_row.get(download_id)
        if row is not None and 0 <= row < len(self._entries):
            return self._entries[row]
        for e in self._all_entries:
            if e.id == download_id:
                return e
        return None

    def row_for_id(self, download_id: str) -> Optional[int]:
        """Row of *download_id* in the currently visible list, or None."""
        row = self._id_to_row.get(download_id)
        if row is not None and 0 <= row < len(self._entries):
            return row
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
                elif e.status in ("paused", "stopped") and downloaded == 0 and e.downloaded_size > 0:
                    pass
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

        # The engine can also stamp the seeding telemetry columns from the poll
        # without any status transition (a session backfilled on upgrade, or the
        # last_seen_complete backstop), so those cells would otherwise keep
        # showing a stale value until the whole table reloaded.
        self.dataChanged.emit(
            self.index(row, Col.LAST_SEEDED),
            self.index(row, Col.SEEDING_STARTED_AT),
            [Qt.ItemDataRole.DisplayRole],
        )

    def _entry_section_id(self, entry: Optional[DownloadEntry]) -> Optional[str]:
        if not entry:
            return None
        if self._segregated_mode == "date":
            return get_entry_date_category(entry)
        elif self._segregated_mode == "type":
            return get_entry_type_category(entry)
        else:
            if entry.status in ACTIVE_SECTION_STATUSES:
                return SECTION_ACTIVE
            elif entry.status in SEEDING_SECTION_STATUSES:
                return SECTION_SEEDING
            elif entry.status in INACTIVE_SECTION_STATUSES:
                return SECTION_INACTIVE
            return SECTION_INACTIVE

    def update_status(self, download_id: str, status: str,
                      error_msg: str = ""):
        entry_all: Optional[DownloadEntry] = None
        old_sec: Optional[str] = None
        old_status: Optional[str] = None
        for e in self._all_entries:
            if e.id == download_id:
                old_status = e.status
                if self._segregated_view:
                    old_sec = self._entry_section_id(e)
                e.status = status
                e.error_message = error_msg
                if status in ("paused", "completed", "error", "stopped"):
                    e.speed = 0
                    e.eta_seconds = 0
                entry_all = e
                break

        if self._segregated_view:
            new_sec = self._entry_section_id(entry_all) if entry_all else None
            was_active = old_status in ACTIVE_QUEUE_STATUSES
            now_active = status in ACTIVE_QUEUE_STATUSES
            if old_sec != new_sec or was_active != now_active:
                self._reapply_filter()
                return True
            row = self._id_to_row.get(download_id)
            if row is not None:
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
            return False

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

    def update_url(self, download_id: str, new_url: str) -> bool:
        """Update entry URL after user refreshes/edits download address."""
        found = False
        for e in self._all_entries:
            if e.id == download_id:
                e.url = new_url
                found = True
                break

        row = self._id_to_row.get(download_id)
        if row is not None:
            self._entries[row].url = new_url
            left = self.index(row, 0)
            right = self.index(row, Col.COUNT - 1)
            self.dataChanged.emit(
                left, right,
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole],
            )
        return found

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

        old_sec: Optional[str] = None
        for i, e in enumerate(self._all_entries):
            if e.id == download_id:
                if self._segregated_view:
                    old_sec = self._entry_section_id(e)
                if entry.downloaded_size == 0 and e.downloaded_size > 0 and entry.status in ("paused", "stopped", "suspended"):
                    entry.downloaded_size = e.downloaded_size
                self._all_entries[i] = entry
                break
        else:
            self._all_entries.append(entry)

        if self._segregated_view:
            new_sec = self._entry_section_id(entry)
            if old_sec is None or old_sec != new_sec:
                self._reapply_filter()
                return True
            row = self._id_to_row.get(download_id)
            if row is not None:
                self._entries[row] = entry
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
            return False

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

        if role == QUEUE_COLOR_ROLE:
            # Read before the section-header branch: a section header is not in a queue, but
            # returning "" for it keeps the delegate on its plain-text path rather than making
            # it guess.
            return self.queue_color_for(entry) if not getattr(
                entry, "is_section_header", False
            ) else ""

        if role == Qt.ItemDataRole.ToolTipRole and col == Col.QUEUE_NAME:
            # The column collapses to the swatch alone when narrow, and a coloured square with
            # no legend is unreadable - so the hover carries the name whether or not the cell
            # had room to show it. Set here rather than in the delegate so it does not depend
            # on the column's width. The colour hex is deliberately left out: nobody identifies
            # a queue by its hex, and it made the tooltip read like a debug field.
            return self.queue_name_for(entry)

        if getattr(entry, "is_section_header", False):
            if role == Qt.ItemDataRole.DisplayRole:
                if col == Col.QUEUE:
                    arrow = "▶" if entry.section_collapsed else "▼"
                    return f"  {arrow}   {entry.section_title.upper()} ({entry.section_count})"
                return ""
            if role == Qt.ItemDataRole.BackgroundRole:
                return QColor("#1e2330")
            if role == Qt.ItemDataRole.ForegroundRole:
                if entry.section_id in (SECTION_ACTIVE, SECTION_DATE_TODAY, SECTION_TYPE_PHOTO):
                    return QColor(Colors.ACCENT)
                elif entry.section_id in (SECTION_SEEDING, SECTION_DATE_YESTERDAY, SECTION_TYPE_AUDIO):
                    return QColor(Colors.PURPLE)
                elif entry.section_id in (SECTION_DATE_LAST_7_DAYS, "date_this_week", SECTION_TYPE_ARCHIVE):
                    return QColor("#ffb74d")
                elif entry.section_id in (SECTION_DATE_LAST_30_DAYS, "date_this_month", SECTION_TYPE_VIDEO):
                    return QColor("#64b5f6")
                elif entry.section_id == SECTION_TYPE_DOCUMENTS:
                    return QColor(Colors.CYAN)
                else:
                    return QColor("#8fa0b5")
            if role == Qt.ItemDataRole.FontRole:
                return fonts.ui_font(10, bold=True)
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
            socks = self._tor_config.socks5_url if self._tor_config else "Tor"
            tor_note = ""
            if is_tor:
                scope = (
                    "this download" if self.is_tor_routed_by_choice(entry)
                    else "all downloads"
                )
                tor_note = f"🧅 Active Tor Route: Routed via SOCKS5 proxy ({socks})\nApplies to: {scope}"
            elif self.is_tor_routed_by_choice(entry):
                tor_note = (
                    "🧅 Tor routing is set for this download, but Tor is not "
                    "running, so it is using the normal route."
                )

            if col == Col.NAME:
                type_tag = f"[{entry.download_type.upper()}] " if entry.download_type else ""
                base = f"{type_tag}{self.get_original_name(entry)}"
                yt_note = self._youtube_note(entry)
                notes = [n for n in (yt_note, tor_note) if n]
                return "\n".join([*notes, base]) if notes else base
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

    @staticmethod
    def _youtube_note(entry: DownloadEntry) -> str:
        """Tooltip line describing a YouTube download's origin and engine."""
        if not is_youtube_entry(entry):
            return ""
        meta = entry.metadata or {}
        mode = "yt-dlp (ffmpeg merged)" if meta.get("youtube_mode") == "b" else "My-IDM direct URL"
        parts = [f"YouTube download via {mode}"]
        if meta.get("uploader"):
            parts.append(str(meta["uploader"]))
        if meta.get("video_id"):
            parts.append(f"id={meta['video_id']}")
        return " — ".join(parts[:2]) + (f"\nVideo id: {meta['video_id']}" if meta.get("video_id") else "")

    def _display_data(self, entry: DownloadEntry, col: int) -> Any:
        if col == Col.QUEUE:
            if getattr(entry, "is_section_header", False) or entry.status not in ACTIVE_QUEUE_STATUSES:
                return ""
            row = self._id_to_row.get(entry.id)
            if row is None:
                return ""
            count = 0
            for i in range(row + 1):
                e = self._entries[i]
                if not getattr(e, "is_section_header", False) and e.status in ACTIVE_QUEUE_STATUSES:
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

        if col == Col.LAST_SEEDED:
            return _format_time(entry.last_seeded_at)

        if col == Col.SOURCE:
            return resolve_download_source(entry) or ""

        if col == Col.SEEDING_STARTED_AT:
            return _format_time(entry.seeding_started_at)

        if col == Col.QUEUE_NAME:
            return self.queue_name_for(entry)

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
