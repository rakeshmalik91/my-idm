"""QAbstractTableModel for the download list view."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Set

import humanize
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    QTimer,
    Signal,
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

# -- Name-based segregation (series/grouping by show name) -----------------
# Groups downloads by show/series name using normalized name matching with
# edit distance threshold.
SECTION_NAME_PREFIX = "name_"

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
SEGREGATED_MODES = ("status", "date", "type", "name")
DEFAULT_SEGREGATED_MODE = "status"

#: Menu / UI labels for the modes, kept beside the modes so the View menu and the
#: Preferences tab cannot drift apart.
SEGREGATED_MODE_LABELS = {
    "status": "Status (Active / Seeding / Inactive)",
    "date": "Date (Today / Yesterday / Last 7 Days / Last 30 Days / Older)",
    "type": "File Type (Video / Audio / Archives / Documents / Photos / General)",
    "name": "Name (Smart Series / Show Grouping)",
}


def split_extension(name: str) -> tuple[str, str]:
    """Split *name* into (stem, extension-with-dot), lower-cased and dot-prefixed.

    Returns ``("", "")`` for a blank name.
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


def _normalize_name_for_grouping(name: str) -> str:
    """Normalize a name for series/grouping comparison.
    
    Strips non-alphanumeric characters (except spaces), lowercases, and collapses whitespace.
    """
    if not name:
        return ""
    # Keep alphanumeric and spaces, replace other chars with space
    normalized = re.sub(r'[^a-zA-Z0-9\s]+', ' ', name)
    # Collapse multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip().lower()


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if len(s2) == 0:
        return len(s1)
    
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def _names_are_similar(name1: str, name2: str, threshold: float = 0.1) -> bool:
    """Check if two normalized names are similar based on edit distance.
    
    Returns True if edit distance is at most threshold * max(len(name1), len(name2)).
    """
    if not name1 or not name2:
        return False
    if name1 == name2:
        return True
    max_len = max(len(name1), len(name2))
    if max_len == 0:
        return True
    distance = _levenshtein_distance(name1, name2)
    return distance <= max_len * threshold


_COMPILED_STRIP_REGEX: Optional[re.Pattern] = None


def _get_strip_keywords_pattern() -> re.Pattern:
    """Return the compiled regex for stripping keywords loaded from assets/strip_keywords.txt."""
    global _COMPILED_STRIP_REGEX
    if _COMPILED_STRIP_REGEX is not None:
        return _COMPILED_STRIP_REGEX

    keywords = []
    assets_file = Path(__file__).resolve().parent.parent / "assets" / "strip_keywords.txt"
    if assets_file.exists():
        try:
            with open(assets_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        keywords.append(line)
        except Exception:
            pass

    if not keywords:
        keywords = [
            "1080p", "720p", "480p", "4k", "2160p", "hd", "sd", "uhd", "hdr",
            "x264", "x265", "h264", "h265", "hevc", "av1", "xvid", "divx",
            "web-dl", "webdl", "webrip", "bluray", "bdrip", "brrip", "dvdrip", "hdrip", "tvrip", "hdtv",
            "animepahe", "repack", "proper", "remastered", "uncut",
        ]

    # Sort keywords by descending length so multi-word or longer keywords match first
    keywords = sorted(set(keywords), key=len, reverse=True)
    escaped = [re.escape(k) for k in keywords]
    pattern_str = r'\b(?:' + '|'.join(escaped) + r')\b'
    _COMPILED_STRIP_REGEX = re.compile(pattern_str, re.IGNORECASE)
    return _COMPILED_STRIP_REGEX


def _extract_show_name(filename: str) -> str:
    """Extract a potential show/series name from a filename.
    
    Attempts to find the show name by removing common episode/season patterns,
    quality tags, release group tags, etc. loaded from assets/strip_keywords.txt.
    Preserves common season identifiers like "Season 4" that are shared across episodes.
    """
    if not filename:
        return ""
    
    # Remove file extension
    stem, _ = split_extension(filename)
    
    # 1. Standard AnimePahe / anime release pattern:
    #    AnimePahe_<Show Name>_-_<Episode>_<Quality>_<Audio>.<ext>
    #    e.g. AnimePahe_Ranma ½ (2024) Season 3_-_25_720p_EngDub.mp4
    #    or AnimePahe_Detective_Conan_-_1214_720p_SubsPlease.mp4
    m_animepahe = re.match(r'^AnimePahe[_\s]+(.+?)[_\s]+-[_\s]+(?:\d+|ep?\d+).*$', stem, re.IGNORECASE)
    if m_animepahe:
        cand = m_animepahe.group(1).strip()
        cand = re.sub(r'[._\-]+', ' ', cand)
        return re.sub(r'\s+', ' ', cand).strip()

    # Common structural patterns to remove
    structural_patterns = [
        # Season/episode patterns - remove specific episode markers but keep season context
        r'\b[Ss]\d{1,2}[Ee]\d{1,2}\b',
        r'\b[Ee]pisode\s*\d+\b',
        r'\b\d{1,2}x\d{1,2}\b',
        r'(?i)[-.]\s*(ep|episode)\s*\d+\s*$',
        r'(?i)[-.]\s*e\d+\s*$',
        r'\(\d{4}\)',
        r'\[[^\]]*\]',
        r'\([^)]*\)',
    ]
    
    result = stem
    for pattern in structural_patterns:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)
    
    # Strip keywords from assets file
    result = _get_strip_keywords_pattern().sub('', result)
    
    # Clean up trailing release group or tags (e.g. - FLUX or [group])
    result = re.sub(r'[-\[\s]([a-zA-Z0-9]{2,})\s*$', '', result)
    
    # Clean up separators
    result = re.sub(r'[._\-]+', ' ', result)
    result = re.sub(r'\s+', ' ', result)
    
    return result.strip()


def _group_entries_by_name(entries: list[DownloadEntry]) -> dict[str, list[DownloadEntry]]:
    """Group entries by show/series name using normalized name matching.
    
    Groups entries that:
    1. Start with the same normalized prefix and have repetitive rest, OR
    2. Have at most 50% edit distance between normalized names
    
    Single-item groups are merged into "Uncategorized".
    
    Returns a dict mapping group name -> list of entries.
    """
    if not entries:
        return {}
    
    # First, extract and normalize names for all entries
    entry_data = []
    for entry in entries:
        # Check metadata for show_title first
        show_title = None
        try:
            meta = getattr(entry, 'metadata', None)
            if meta:
                show_title = (meta.get('show_title') or 
                             meta.get('series_title') or 
                             meta.get('series_name') or 
                             meta.get('anime_title') or 
                             meta.get('title') or 
                             meta.get('name'))
        except Exception:
            pass
        
        if show_title:
            # Use show_title from metadata as the primary grouping key
            # Remove strip keywords from show_title for proper grouping
            show_title_clean = _get_strip_keywords_pattern().sub('', show_title)
            show_title_clean = re.sub(r'[._\-]+', ' ', show_title_clean)
            show_title_clean = re.sub(r'\s+', ' ', show_title_clean).strip()
            display_name = show_title_clean  # Preserve original case for display
            show_name = show_title_clean
            normalized = _normalize_name_for_grouping(show_name)
        else:
            # Use filename as the primary name source
            filename = getattr(entry, 'filename', '') or getattr(entry, 'name', '') or ''
            show_name = _extract_show_name(filename)
            display_name = show_name  # Use extracted name for display
            normalized = _normalize_name_for_grouping(show_name) if show_name else _normalize_name_for_grouping(filename)
        entry_data.append((entry, normalized, display_name, show_name or (getattr(entry, 'filename', '') or getattr(entry, 'name', '') or '')))
    
    # Group similar names
    groups: dict[str, list[DownloadEntry]] = {}
    norm_to_key: dict[str, str] = {}
    used = set()
    
    for i, (entry_i, norm_i, display_i, orig_i) in enumerate(entry_data):
        if i in used:
            continue
        
        # Find all similar entries
        group_entries = [entry_i]
        group_names = [display_i]  # Use display names for grouping logic
        used.add(i)
        
        for j, (entry_j, norm_j, display_j, orig_j) in enumerate(entry_data):
            if j in used or i == j:
                continue
            
            # Check if names are similar
            if _names_are_similar(norm_i, norm_j):
                group_entries.append(entry_j)
                group_names.append(display_j if display_j else orig_j)
                used.add(j)
        
        # Determine group name:
        # If any entry in the group has an explicit show/anime title in metadata,
        # prefer that clean title as the group key.
        explicit_title = None
        for ge in group_entries:
            meta = getattr(ge, 'metadata', None)
            if meta:
                t = (meta.get('show_title') or meta.get('series_title') or
                     meta.get('series_name') or meta.get('anime_title'))
                if t and str(t).strip():
                    explicit_title = str(t).strip()
                    break

        if explicit_title:
            group_key = explicit_title
        elif len(group_entries) == 1:
            group_key = display_i if display_i else (orig_i if orig_i else norm_i)
        else:
            # Find common prefix among group names
            group_key = _find_common_prefix([n for n in group_names if n])
            if not group_key:
                group_key = min(group_names, key=len)
        
        # Clean up group key: remove strip keywords (PSA, 720p, etc.) for display
        group_key = _get_strip_keywords_pattern().sub('', group_key)
        group_key = re.sub(r'[._\-]+', ' ', group_key)
        group_key = re.sub(r'\s+', ' ', group_key).strip()
        
        group_key = group_key or f"Group {len(groups) + 1}"
        norm_key = _normalize_name_for_grouping(group_key)
        if norm_key in norm_to_key:
            existing_key = norm_to_key[norm_key]
            groups[existing_key].extend(group_entries)
            if sum(1 for c in group_key if c.isupper()) > sum(1 for c in existing_key if c.isupper()):
                groups[group_key] = groups.pop(existing_key)
                norm_to_key[norm_key] = group_key
        else:
            norm_to_key[norm_key] = group_key
            groups[group_key] = group_entries
    
    # Handle any remaining ungrouped entries
    for i, (entry_i, norm_i, display_i, orig_i) in enumerate(entry_data):
        if i not in used:
            group_key = display_i if display_i else (orig_i if orig_i else norm_i)
            if not group_key:
                group_key = f"Ungrouped {len(groups) + 1}"
            norm_key = _normalize_name_for_grouping(group_key)
            if norm_key in norm_to_key:
                existing_key = norm_to_key[norm_key]
                groups[existing_key].append(entry_i)
            else:
                norm_to_key[norm_key] = group_key
                groups[group_key] = [entry_i]
    
    # Merge single-item groups into "Uncategorized" - but keep entries with show_title as their own groups
    multi_item_groups = {k: v for k, v in groups.items() if len(v) > 1}
    multi_norm_to_key = {_normalize_name_for_grouping(k): k for k in multi_item_groups}
    single_items = []
    
    for k, v in groups.items():
        if len(v) == 1:
            entry = v[0]
            # Check if this entry has a show/series title in metadata.
            # Only use show_title, series_title, series_name, and anime_title — NOT the generic
            # 'title' or 'name' keys, which any download might have and would cause
            # false positives that keep random single downloads out of Uncategorized.
            has_show_title = False
            try:
                meta = getattr(entry, 'metadata', None)
                if meta:
                    has_show_title = bool(meta.get('show_title') or meta.get('series_title') or meta.get('series_name') or meta.get('anime_title'))
            except Exception:
                pass
            
            norm_k = _normalize_name_for_grouping(k)
            if norm_k in multi_norm_to_key:
                target_key = multi_norm_to_key[norm_k]
                multi_item_groups[target_key].extend(v)
            elif has_show_title:
                multi_item_groups[k] = v
                multi_norm_to_key[norm_k] = k
            else:
                single_items.extend(v)
    
    if single_items:
        multi_item_groups["Uncategorized"] = single_items

    # Final pass: merge any groups whose normalized names match
    final_groups: dict[str, list[DownloadEntry]] = {}
    final_norm_to_key: dict[str, str] = {}
    for k, v in multi_item_groups.items():
        if k == "Uncategorized":
            if "Uncategorized" in final_groups:
                final_groups["Uncategorized"].extend(v)
            else:
                final_groups["Uncategorized"] = list(v)
            continue
        norm_k = _normalize_name_for_grouping(k)
        if norm_k in final_norm_to_key:
            target_k = final_norm_to_key[norm_k]
            final_groups[target_k].extend(v)
            if sum(1 for c in k if c.isupper()) > sum(1 for c in target_k if c.isupper()):
                final_groups[k] = final_groups.pop(target_k)
                final_norm_to_key[norm_k] = k
        else:
            final_norm_to_key[norm_k] = k
            final_groups[k] = list(v)
    
    return final_groups


def _find_common_prefix(names: list[str]) -> str:
    """Find the longest common prefix among a list of names.
    
    Returns a prefix that ends at a word boundary (space, dash, underscore, dot)
    to avoid returning incomplete words like "Season 4 - e0".
    """
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    
    # Sort to bring most different names to ends
    names = sorted(names, key=len)
    shortest = names[0]
    
    for i in range(len(shortest), 0, -1):
        prefix = shortest[:i]
        if all(n.startswith(prefix) for n in names):
            # Clean up the prefix - remove trailing separators
            prefix = re.sub(r'[\s\-\._]+$', '', prefix)
            if len(prefix) < 3:  # Minimum meaningful prefix length
                continue
            # Check if the prefix ends in the middle of a word by looking at the next
            # character in the SHORTEST string - if it's alphanumeric, we cut off a word
            cut_mid_word = False
            if len(shortest) > len(prefix):
                next_char = shortest[len(prefix)]
                if next_char.isalnum():
                    cut_mid_word = True
            if cut_mid_word:
                # Find last word boundary in prefix
                last_boundary = max(
                    prefix.rfind(' '),
                    prefix.rfind('-'),
                    prefix.rfind('_'),
                    prefix.rfind('.')
                )
                if last_boundary >= 3:
                    prefix = prefix[:last_boundary + 1].rstrip(' -_.')
                    if len(prefix) >= 3:
                        return prefix
            else:
                # Prefix ends at a word boundary naturally
                return prefix
    return ""


import re


class DownloadTableModel(QAbstractTableModel):
    """Table model backed by a list of DownloadEntry objects with filtering support."""

    queue_filter_changed = Signal(object)  # Optional[Set[str]]

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
        self._name_sections_auto_collapsed: bool = False
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
        # Download IDs pending paced batch deletion. Rows in this set are dimmed and disabled.
        self._deleting_ids: set[str] = set()
        # Cached groups for segregated view: list of (sec_id, title, group_entries, hdr_id)
        self._cached_segregated_groups: Optional[list[tuple[str, str, list[DownloadEntry], str]]] = None
        self._cached_name_groups: Optional[dict[str, list[DownloadEntry]]] = None
        # Reverse lookup: entry.id -> section_id for name mode. Built from the actual
        # grouping result so _entry_section_id does not need to recompute independently
        # (which could mismatch due to edit-distance grouping).
        self._name_section_map: dict[str, str] = {}
        # Explicitly expanded sections in modes that are collapsed by default (such as "name" mode)
        self._expanded_sections: set[str] = set()

    def _invalidate_grouping_cache(self):
        """Invalidate the cached segregated groups so they are recomputed on next state change."""
        self._cached_segregated_groups = None
        self._cached_name_groups = None
        self._name_section_map = {}

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
        self._invalidate_grouping_cache()
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
        # Reset auto-collapse flag when segregation is enabled
        if enabled:
            self._name_sections_auto_collapsed = False
        self._reapply_filter()

    def is_segregated_view(self) -> bool:
        return self._segregated_view

    def set_segregated_mode(self, mode: str):
        if mode not in SEGREGATED_MODES:
            mode = DEFAULT_SEGREGATED_MODE
        if self._segregated_mode != mode:
            self._segregated_mode = mode
            # Reset auto-collapse flag when mode changes
            self._name_sections_auto_collapsed = False
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

    def _build_entries_from_cached_groups(self):
        """Build self._entries from self._cached_segregated_groups without recalculating grouping."""
        if self._cached_segregated_groups is None:
            return

        entries_by_id = {e.id: e for e in self._all_entries}

        entries: list[DownloadEntry] = []
        for sec_id, title, group_entries, hdr_id in self._cached_segregated_groups:
            # Sync group_entries with canonical entries in _all_entries so in-place status/progress
            # and fresh entry updates are reflected accurately when expanding/collapsing.
            for idx, e in enumerate(group_entries):
                canonical = entries_by_id.get(e.id)
                if canonical is not None:
                    group_entries[idx] = canonical

            is_col = self.is_section_collapsed(sec_id)

            active_count = sum(1 for e in group_entries if e.status in ACTIVE_QUEUE_STATUSES)
            seeding_count = sum(1 for e in group_entries if e.status == "seeding")
            active_entries = [e for e in group_entries if e.status in ACTIVE_QUEUE_STATUSES]
            active_progress = 0.0
            if active_entries:
                total_size = sum(e.total_size for e in active_entries if e.total_size > 0)
                downloaded_size = sum(e.downloaded_size for e in active_entries if e.downloaded_size > 0)
                if total_size > 0:
                    active_progress = min(100.0, (downloaded_size / total_size) * 100.0)

            hdr = DownloadEntry(
                id=hdr_id,
                is_section_header=True,
                status="section_header",
                section_id=sec_id,
                section_title=title,
                section_count=len(group_entries),
                section_active_count=active_count,
                section_seeding_count=seeding_count,
                section_active_progress=active_progress,
                section_collapsed=is_col,
            )
            entries.append(hdr)
            if not is_col:
                entries.extend(group_entries)

        self._entries = entries

    def set_section_collapsed(self, section_id: str, collapsed: bool):
        if collapsed:
            self._collapsed_sections.add(section_id)
        else:
            self._collapsed_sections.discard(section_id)
            # Prevent auto-collapse from re-adding this section
            if section_id.startswith(SECTION_NAME_PREFIX):
                self._name_sections_auto_collapsed = True

        if self._cached_segregated_groups is not None:
            # Fast path: rebuild the visible entry list from the cached groups without
            # recomputing grouping. The group entries are the same Python objects as in
            # _all_entries, so in-place status/progress updates are already reflected.
            self.beginResetModel()
            self._build_entries_from_cached_groups()
            self._rebuild_index()
            self.endResetModel()
        else:
            # Cache is None (should not happen in normal segregated view, but can if
            # entries were added/removed since last rebuild). Rebuild the cache first,
            # then use the fast path to avoid a full regrouping which can reorder
            # entries within groups (active vs inactive split).
            if self._segregated_view:
                self._cached_segregated_groups = None
                self._cached_name_groups = None
                self._apply_sort()
                self.beginResetModel()
                self._build_entries_from_cached_groups()
                self._rebuild_index()
                self.endResetModel()
            else:
                self._reapply_filter()

    def is_section_collapsed(self, section_id: str) -> bool:
        return section_id in self._collapsed_sections

    def is_section_header_row(self, row: int) -> bool:
        if 0 <= row < len(self._entries):
            return bool(getattr(self._entries[row], "is_section_header", False))
        return False

    def get_section_header_row_indices(self) -> list[int]:
        return [i for i, e in enumerate(self._entries) if getattr(e, "is_section_header", False)]

    def collapse_all_sections(self):
        """Collapse all sections in the current segregated view."""
        if not self._segregated_view:
            return
        if self._cached_segregated_groups:
            for sec_id, _, _, _ in self._cached_segregated_groups:
                self._collapsed_sections.add(sec_id)
        for e in self._entries:
            if getattr(e, "is_section_header", False):
                self._collapsed_sections.add(e.section_id)

        if self._cached_segregated_groups is not None:
            self.beginResetModel()
            self._build_entries_from_cached_groups()
            self._rebuild_index()
            self.endResetModel()
        else:
            self._reapply_filter()

    def expand_all_sections(self):
        """Expand all sections in the current segregated view."""
        if not self._segregated_view:
            return
        self._collapsed_sections.clear()

        if self._cached_segregated_groups is not None:
            self.beginResetModel()
            self._build_entries_from_cached_groups()
            self._rebuild_index()
            self.endResetModel()
        else:
            self._reapply_filter()

    def toggle_section_collapsed(self, row: int) -> Optional[tuple[str, bool]]:
        if 0 <= row < len(self._entries):
            e = self._entries[row]
            if getattr(e, "is_section_header", False):
                sec_id = e.section_id
                now_collapsed = not self.is_section_collapsed(sec_id)
                self.set_section_collapsed(sec_id, now_collapsed)
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
        if (
            allowed_ids is not None
            and len(self._queue_names) > 0
            and len(allowed_ids) >= len(self._queue_names)
        ):
            # Everything ticked is the same as nothing ticked, which is how Select All reads.
            allowed_ids = None
        new_val = set(allowed_ids) if allowed_ids is not None else None
        if self._queue_filter == new_val:
            return
        self._queue_filter = new_val
        self._reapply_filter()
        self.queue_filter_changed.emit(self._queue_filter)

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
        had_q_filter = self._queue_filter is not None
        self._status_filter = None
        self._type_filter = None
        self._size_filter = None
        self._queue_filter = None
        self._reapply_filter()
        if had_q_filter:
            self.queue_filter_changed.emit(None)

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
        self._invalidate_grouping_cache()
        self.beginResetModel()
        self._deleting_ids.clear()
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
        self._deleting_ids.discard(download_id)
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

    def mark_deleting(self, download_ids: Iterable[str]) -> None:
        """Mark download entries as pending deletion, dimming and disabling them."""
        ids = set(download_ids)
        if not ids:
            return
        self._deleting_ids.update(ids)
        for did in ids:
            row = self._id_to_row.get(did)
            if row is not None and 0 <= row < len(self._entries):
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

    def unmark_deleting(self, download_id: str) -> None:
        """Unmark a download entry from pending deletion state."""
        if download_id in self._deleting_ids:
            self._deleting_ids.discard(download_id)
            row = self._id_to_row.get(download_id)
            if row is not None and 0 <= row < len(self._entries):
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

    def is_deleting(self, download_id: str) -> bool:
        """True when the download is queued for paced batch deletion."""
        return download_id in self._deleting_ids

    def is_deleting_row(self, row: int) -> bool:
        """True when the entry at visible *row* is queued for deletion."""
        if 0 <= row < len(self._entries):
            return self._entries[row].id in self._deleting_ids
        return False

    # -- sorting -------------------------------------------------------------

    def sort(self, column: int, order: Optional[Qt.SortOrder] = None):
        """Sort the model by the specified column and order."""
        if order is None:
            order = Qt.SortOrder.DescendingOrder if column == Col.ADDED else Qt.SortOrder.AscendingOrder
        self._sort_column = column
        self._sort_order = order
        self._cached_segregated_groups = None
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

        if self._cached_segregated_groups is None:
            buckets: dict[str, list[DownloadEntry]]
            if self._segregated_mode == "date":
                # Grouped view: group by Today, Yesterday, Last 7 Days, Last 30 Days, Older.
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
                # Grouped view: group by Active, Seeding, Inactive, or by file type, or by name.
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
                elif self._segregated_mode == "name":
                    # Group by show/series name using normalized name matching
                    if self._cached_name_groups is None:
                        self._cached_name_groups = _group_entries_by_name(filtered)
                        # Build the reverse lookup: entry.id -> section_id so that
                        # _entry_section_id can return the *actual* group assignment
                        # rather than recomputing independently (which can mismatch
                        # due to edit-distance grouping).
                        self._name_section_map = {}
                        for key, entries_in_group in self._cached_name_groups.items():
                            sec_id = f"{SECTION_NAME_PREFIX}{key}"
                            for e in entries_in_group:
                                self._name_section_map[e.id] = sec_id
                    else:
                        entries_by_id = {e.id: e for e in filtered}
                        for key, grp_entries in self._cached_name_groups.items():
                            for idx, e in enumerate(grp_entries):
                                if e.id in entries_by_id:
                                    grp_entries[idx] = entries_by_id[e.id]
                    # Sort groups alphabetically (including Uncategorized)
                    sorted_keys = sorted(self._cached_name_groups.keys(), key=str.lower)
                    groups = [
                        (f"{SECTION_NAME_PREFIX}{key}", key, list(self._cached_name_groups[key]), f"__section_name_{key}__")
                        for key in sorted_keys
                    ]
                    # Collapse name-based sections by default (only on first creation with entries, not on rebuild)
                    if not self._name_sections_auto_collapsed and groups:
                        for sec_id, _, _, _ in groups:
                            if sec_id not in self._collapsed_sections:
                                self._collapsed_sections.add(sec_id)
                        self._name_sections_auto_collapsed = True
                else:
                    active_entries = [e for e in filtered if e.status in ACTIVE_SECTION_STATUSES]
                    seeding_entries = [e for e in filtered if e.status in SEEDING_SECTION_STATUSES]
                    inactive_entries = [e for e in filtered if e.status in INACTIVE_SECTION_STATUSES]

                    groups = [
                        (SECTION_ACTIVE, "Active", active_entries, "__section_active__"),
                        (SECTION_SEEDING, "Seeding", seeding_entries, "__section_seeding__"),
                        (SECTION_INACTIVE, "Inactive", inactive_entries, "__section_inactive__"),
                    ]
        else:
            groups = self._cached_segregated_groups

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

        if self._segregated_mode == "name":
            def _group_sort_key(grp: tuple[str, str, list[DownloadEntry], str]) -> Any:
                _sec_id, title, group_entries, _hdr_id = grp
                if not group_entries:
                    return (0, "")
                if self._sort_column is None or self._sort_column == Col.NAME:
                    return title.lower()
                if self._sort_column == Col.SIZE:
                    return sum(e.total_size for e in group_entries if e.total_size > 0)
                if self._sort_column == Col.SPEED:
                    return sum(e.speed for e in group_entries if e.status == "downloading")
                if self._sort_column == Col.PROGRESS:
                    tot = sum(e.total_size for e in group_entries if e.total_size > 0)
                    don = sum(e.downloaded_size for e in group_entries if e.downloaded_size > 0)
                    return (don / tot) if tot > 0 else 0.0
                return self._entry_sort_key(group_entries[0], self._sort_column, ascending)

            # Sort all groups uniformly — Uncategorized participates in the same
            # sort order as every other group instead of being pinned at the bottom.
            groups.sort(key=_group_sort_key, reverse=reverse)

        self._cached_segregated_groups = groups
        self._build_entries_from_cached_groups()

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

    def _find_section_header_row(self, section_id: str) -> Optional[int]:
        """Find the row index of the section header for the given section_id."""
        for i, e in enumerate(self._entries):
            if getattr(e, "is_section_header", False) and getattr(e, "section_id", None) == section_id:
                return i
        return None

    def get_section_download_rows(self, section_id: str) -> list[int]:
        """Visible row indices of downloads belonging to *section_id*."""
        hdr_idx = None
        for i, e in enumerate(self._entries):
            if getattr(e, "is_section_header", False) and getattr(e, "section_id", None) == section_id:
                hdr_idx = i
                break
        if hdr_idx is None:
            return []
        rows: list[int] = []
        for r in range(hdr_idx + 1, len(self._entries)):
            if getattr(self._entries[r], "is_section_header", False):
                break
            if self._entries[r].id not in self._deleting_ids:
                rows.append(r)
        return rows

    def get_section_download_ids(self, section_id: str) -> list[str]:
        """Download IDs belonging to *section_id* in the current visible list."""
        rows = self.get_section_download_rows(section_id)
        return [self._entries[r].id for r in rows if 0 <= r < len(self._entries)]

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
            if 0 <= r < len(self._entries)
            and not getattr(self._entries[r], "is_section_header", False)
            and self._entries[r].id not in self._deleting_ids
        ]

    @property
    def entries(self) -> list[DownloadEntry]:
        return self._entries

    @property
    def all_entries(self) -> list[DownloadEntry]:
        return self._all_entries

    def get_aggregate_speeds(self) -> tuple[float, float]:
        """Returns (total_download_speed, total_upload_speed) in B/s."""
        down = sum(e.speed for e in self._all_entries if e.status == "downloading" and e.id not in self._deleting_ids)
        up = sum(e.upload_speed for e in self._all_entries if e.status in ("downloading", "seeding") and e.id not in self._deleting_ids)
        return down, up

    # -- progress updates (called from manager signals) ---------------------

    def update_progress(self, download_id: str, downloaded: int,
                        total: int, speed: float, eta: float,
                        seeds: int = 0, peers: int = 0,
                        upload_speed: float = 0.0,
                        total_seeds: int = 0, total_peers: int = 0):
        if download_id in self._deleting_ids:
            return
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

        # Find the entry in _all_entries to get its section_id and update section header progress
        entry_obj = None
        for e in self._all_entries:
            if e.id == download_id:
                entry_obj = e
                break
        
        row = self._id_to_row.get(download_id)
        
        # If in segregated view mode, update the section header's progress bar
        if self._segregated_view and entry_obj:
            section_id = self._entry_section_id(entry_obj)
            if section_id:
                hdr_row = self._find_section_header_row(section_id)
                if hdr_row is not None:
                    # Recalculate section active progress from _all_entries (includes collapsed sections)
                    active_entries = [
                        e for e in self._all_entries
                        if self._entry_section_id(e) == section_id and e.status in ACTIVE_QUEUE_STATUSES
                    ]
                    # Calculate consolidated progress
                    if active_entries:
                        total_size = sum(e.total_size for e in active_entries if e.total_size > 0)
                        downloaded_size = sum(e.downloaded_size for e in active_entries if e.downloaded_size > 0)
                        if total_size > 0:
                            progress = min(100.0, (downloaded_size / total_size) * 100.0)
                        else:
                            progress = 0.0
                    else:
                        progress = 0.0
                    
                    hdr_entry = self._entries[hdr_row]
                    if hdr_entry.section_id == section_id:
                        hdr_entry.section_active_progress = progress
                    
                    # Emit dataChanged for the section header row
                    hdr_left = self.index(hdr_row, Col.QUEUE)
                    hdr_right = self.index(hdr_row, Col.SEEDS_PEERS)
                    self.dataChanged.emit(hdr_left, hdr_right, [Qt.ItemDataRole.DisplayRole])

        # Emit change for the entry row if visible
        if row is not None:
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
        elif self._segregated_mode == "name":
            # Use the reverse lookup built from the actual grouping result.
            # The old approach recomputed the section_id independently from the
            # entry's filename/show_title, but that could mismatch the group that
            # edit-distance matching actually placed the entry into — causing
            # progress bar updates to target the wrong (or non-existent) header.
            if entry.id in self._name_section_map:
                return self._name_section_map[entry.id]
            # Fallback for entries added after the last grouping rebuild
            # (shouldn't normally happen because _reapply_filter rebuilds the map).
            show_title = None
            try:
                meta = getattr(entry, 'metadata', None)
                if meta:
                    show_title = (meta.get('show_title') or
                                 meta.get('series_title') or
                                 meta.get('series_name'))
            except Exception:
                pass
            if show_title:
                show_title_clean = _get_strip_keywords_pattern().sub('', show_title)
                show_title_clean = re.sub(r'[._\-]+', ' ', show_title_clean)
                show_title_clean = re.sub(r'\s+', ' ', show_title_clean).strip()
                return f"{SECTION_NAME_PREFIX}{show_title_clean}"
            else:
                filename = getattr(entry, 'filename', '') or getattr(entry, 'name', '') or ''
                return f"{SECTION_NAME_PREFIX}{_extract_show_name(filename)}"
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
        if download_id in self._deleting_ids:
            return False
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

        # Also update any instances in cached groups
        if self._cached_segregated_groups:
            for sec_id, title, group_entries, hdr_id in self._cached_segregated_groups:
                for ge in group_entries:
                    if ge.id == download_id:
                        ge.status = status
                        ge.error_message = error_msg
                        if status in ("paused", "completed", "error", "stopped"):
                            ge.speed = 0
                            ge.eta_seconds = 0
        if self._cached_name_groups:
            for group_entries in self._cached_name_groups.values():
                for ge in group_entries:
                    if ge.id == download_id:
                        ge.status = status
                        ge.error_message = error_msg
                        if status in ("paused", "completed", "error", "stopped"):
                            ge.speed = 0
                            ge.eta_seconds = 0

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

        if self._segregated_view and self._segregated_mode == "name":
            self._reapply_filter()
            return

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

        if self._segregated_view and self._segregated_mode == "name":
            self._reapply_filter()
            return

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

        # Keep cached group entries pointing to the refreshed entry object
        if self._cached_segregated_groups:
            for sec_id, title, group_entries, hdr_id in self._cached_segregated_groups:
                for idx, ge in enumerate(group_entries):
                    if ge.id == download_id:
                        group_entries[idx] = entry
        if self._cached_name_groups:
            for group_entries in self._cached_name_groups.values():
                for idx, ge in enumerate(group_entries):
                    if ge.id == download_id:
                        group_entries[idx] = entry

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
                elif entry.section_id.startswith(SECTION_NAME_PREFIX):
                    # Uncategorized group in purple, other name groups in cyan
                    if entry.section_id.endswith("Uncategorized"):
                        return QColor(Colors.PURPLE)
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
            if entry.id in self._deleting_ids:
                return QColor(Colors.TEXT_DISABLED)
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
        if 0 <= index.row() < len(self._entries):
            entry = self._entries[index.row()]
            if getattr(entry, "is_section_header", False):
                return Qt.ItemFlag.ItemIsEnabled
            if entry.id in self._deleting_ids:
                return Qt.ItemFlag.NoItemFlags
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
            if entry.id in self._deleting_ids:
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
                "status": "deleting" if entry.id in self._deleting_ids else entry.status,
            }

        if col == Col.STATUS:
            if entry.id in self._deleting_ids:
                return "Deleting..."
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
            if entry.id in self._deleting_ids:
                return "—"
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
            if entry.id in self._deleting_ids:
                return "—"
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
