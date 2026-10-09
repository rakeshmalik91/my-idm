"""SQLite database layer for download history and state persistence."""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from my_idm.paths import data_dir, database_path
from my_idm.utils import normalize_path, to_int


#: Retained as module attributes because many modules import these names directly. The values are
#: resolved once, at import, through `my_idm.paths` - which prefers a pre-existing `~/.my-idm` over
#: the platform location so an existing install never moves its download history.
#: See my_idm/paths.py for the resolution rules.
APP_DIR = data_dir()
DB_PATH = database_path()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: Statuses that mean "the payload is on disk". `seeding` counts: those downloads *are*
#: complete, they just happen to still be uploading.
COMPLETE_STATUSES = ("completed", "seeding")


@dataclass(frozen=True)
class DownloadStats:
    """One bucket of download/upload totals. All byte figures are raw bytes, not strings."""
    count: int = 0
    downloaded: int = 0
    uploaded: int = 0
    completed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "count": self.count,
            "downloaded": self.downloaded,
            "uploaded": self.uploaded,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DownloadStats:
        return cls(
            count=int(data.get("count", 0)),
            downloaded=int(data.get("downloaded", 0)),
            uploaded=int(data.get("uploaded", 0)),
            completed=int(data.get("completed", 0)),
        )

    def __add__(self, other: DownloadStats) -> DownloadStats:
        return DownloadStats(
            count=self.count + other.count,
            downloaded=self.downloaded + other.downloaded,
            uploaded=self.uploaded + other.uploaded,
            completed=self.completed + other.completed,
        )


@dataclass(frozen=True)
class StatsSnapshot:
    """Every bucket the statistics popup shows, read in one pass.

    The summary buckets are keyed on ``added_at``, the only timestamp written exactly once
    and never re-stamped, so the figures do not move when a download is later paused or
    re-checked. ``series`` drives the chart and follows the range/granularity the user
    picked there; its labels are ``YYYY-MM-DD`` or ``YYYY-MM`` depending on ``bucket``.
    """
    today: DownloadStats = field(default_factory=DownloadStats)
    week: DownloadStats = field(default_factory=DownloadStats)
    month: DownloadStats = field(default_factory=DownloadStats)
    year: DownloadStats = field(default_factory=DownloadStats)
    lifetime: DownloadStats = field(default_factory=DownloadStats)
    series: tuple[tuple[str, DownloadStats], ...] = ()
    bucket: str = "day"
    since: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "today": self.today.to_dict(),
            "week": self.week.to_dict(),
            "month": self.month.to_dict(),
            "year": self.year.to_dict(),
            "lifetime": self.lifetime.to_dict(),
            "series": [[label, s.to_dict()] for label, s in self.series],
            "bucket": self.bucket,
            "since": self.since,
        }


def fill_series_gaps(
    series: Sequence[tuple[str, DownloadStats]],
    bucket: str,
    start: Optional[date | datetime | str] = None,
    end: Optional[date | datetime | str] = None,
) -> tuple[tuple[str, DownloadStats], ...]:
    """Fill gaps in a time series with zero DownloadStats so the time axis is continuous.

    Days/hours/minutes with no activity must not be dropped: dropping them compresses the
    time axis and distorts temporal spacing.
    """
    valid_buckets = ("5min", "minute", "hour", "day", "month")
    bucket = bucket if bucket in valid_buckets else "day"
    if bucket == "minute":
        bucket = "5min"

    data_map = dict(series)

    def to_date(val: Any) -> Optional[date]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        if isinstance(val, str):
            try:
                return datetime.fromisoformat(val[:10]).date()
            except Exception:
                return None
        return None

    def to_datetime(val: Any) -> Optional[datetime]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.astimezone().replace(tzinfo=None) if val.tzinfo is not None else val
        if isinstance(val, date):
            return datetime(val.year, val.month, val.day)
        if isinstance(val, str):
            clean = val.replace(" ", "T")
            if len(clean) == 13:
                clean += ":00:00"
            elif len(clean) == 16:
                clean += ":00"
            try:
                dt = datetime.fromisoformat(clean)
                return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt
            except Exception:
                return None
        return None

    if not series and start is None:
        return ()

    if bucket == "month":
        start_dt = to_date(start)
        end_dt = to_date(end) or date.today()
        if start_dt is None:
            if series:
                try:
                    parts = [int(p) for p in series[0][0].split("-")]
                    start_year, start_month = parts[0], parts[1]
                except Exception:
                    return tuple(series)
            else:
                return ()
        else:
            start_year, start_month = start_dt.year, start_dt.month

        end_year, end_month = end_dt.year, end_dt.month
        if series:
            try:
                last_parts = [int(p) for p in series[-1][0].split("-")]
                if (last_parts[0], last_parts[1]) > (end_year, end_month):
                    end_year, end_month = last_parts[0], last_parts[1]
            except Exception:
                pass

        total_months = (end_year - start_year) * 12 + (end_month - start_month) + 1
        if total_months <= 0 or total_months > 50000:
            return tuple(series)

        result = []
        cy, cm = start_year, start_month
        for _ in range(total_months):
            key = f"{cy:04d}-{cm:02d}"
            result.append((key, data_map.get(key, DownloadStats())))
            if cm == 12:
                cy += 1
                cm = 1
            else:
                cm += 1
        return tuple(result)

    elif bucket == "day":
        start_d = to_date(start)
        end_d = to_date(end) or date.today()
        if start_d is None:
            if series:
                start_d = to_date(series[0][0])
            if start_d is None:
                return ()
        if series:
            last_d = to_date(series[-1][0])
            if last_d and last_d > end_d:
                end_d = last_d

        total_days = (end_d - start_d).days + 1
        if total_days <= 0 or total_days > 50000:
            return tuple(series)

        result = []
        cur = start_d
        for _ in range(total_days):
            key = cur.strftime("%Y-%m-%d")
            result.append((key, data_map.get(key, DownloadStats())))
            cur += timedelta(days=1)
        return tuple(result)

    elif bucket == "hour":
        start_dt = to_datetime(start)
        end_dt = to_datetime(end)
        if end_dt is None:
            today_d = to_date(end) or date.today()
            end_dt = datetime(today_d.year, today_d.month, today_d.day, 23, 0)
        else:
            if isinstance(end, date) and not isinstance(end, datetime):
                end_dt = datetime(end.year, end.month, end.day, 23, 0)
            else:
                end_dt = end_dt.replace(minute=0, second=0, microsecond=0)

        if start_dt is None:
            if series:
                start_dt = to_datetime(series[0][0])
            if start_dt is None:
                return ()
        else:
            start_dt = start_dt.replace(minute=0, second=0, microsecond=0)

        if series:
            last_dt = to_datetime(series[-1][0])
            if last_dt and last_dt > end_dt:
                end_dt = last_dt.replace(minute=0, second=0, microsecond=0)

        total_hours = int((end_dt - start_dt).total_seconds() // 3600) + 1
        if total_hours <= 0 or total_hours > 50000:
            return tuple(series)

        result = []
        cur = start_dt
        for _ in range(total_hours):
            key = cur.strftime("%Y-%m-%d %H")
            result.append((key, data_map.get(key, DownloadStats())))
            cur += timedelta(hours=1)
        return tuple(result)

    else:  # "5min"
        start_dt = to_datetime(start)
        end_dt = to_datetime(end)
        if end_dt is None:
            today_d = to_date(end) or date.today()
            end_dt = datetime(today_d.year, today_d.month, today_d.day, 23, 55)
        else:
            if isinstance(end, date) and not isinstance(end, datetime):
                end_dt = datetime(end.year, end.month, end.day, 23, 55)
            else:
                m = (end_dt.minute // 5) * 5
                end_dt = end_dt.replace(minute=m, second=0, microsecond=0)

        if start_dt is None:
            if series:
                start_dt = to_datetime(series[0][0])
            if start_dt is None:
                return ()
        else:
            m = (start_dt.minute // 5) * 5
            start_dt = start_dt.replace(minute=m, second=0, microsecond=0)

        if series:
            last_dt = to_datetime(series[-1][0])
            if last_dt:
                lm = (last_dt.minute // 5) * 5
                last_dt = last_dt.replace(minute=lm, second=0, microsecond=0)
                if last_dt > end_dt:
                    end_dt = last_dt

        total_slots = int((end_dt - start_dt).total_seconds() // 300) + 1
        if total_slots <= 0 or total_slots > 50000:
            return tuple(series)

        result = []
        cur = start_dt
        for _ in range(total_slots):
            key = cur.strftime("%Y-%m-%d %H:%M")
            result.append((key, data_map.get(key, DownloadStats())))
            cur += timedelta(minutes=5)
        return tuple(result)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


class _LockedCursor:
    """Cursor proxy that re-acquires the connection lock while fetching.

    `execute()` releases the lock as soon as the statement returns, but callers fetch
    afterwards, so the fetch has to be guarded separately or a second thread can run a
    statement between the two. Iteration is materialised eagerly, which is what every
    caller in this module does anyway.
    """

    __slots__ = ("_cursor", "_lock")

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock):
        self._cursor = cursor
        self._lock = lock

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchall(self):
        with self._lock:
            return self._cursor.fetchall()

    def fetchmany(self, size: Optional[int] = None):
        with self._lock:
            if size is None:
                return self._cursor.fetchmany()
            return self._cursor.fetchmany(size)

    def __iter__(self):
        return iter(self.fetchall())

    def __len__(self):
        with self._lock:
            return len(self._cursor.fetchall())

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._cursor.close()
        return False

    def __getattr__(self, name):
        attr = getattr(self._cursor, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        def guarded(*args, **kwargs):
            with self._lock:
                return attr(*args, **kwargs)

        return guarded


class _ClosedCursor:
    """Cursor returned once the connection has been closed.

    Teardown races are unavoidable in this application: worker threads (`scan-*`,
    `ytdlp-download`, the asyncio loop) can be one statement away from finishing when
    the manager shuts the engine down. Before this existed, that late statement raised
    ``sqlite3.ProgrammingError: Cannot operate on a closed database`` from a background
    thread, which surfaced as random test failures and as stderr noise at exit. A closed
    database now degrades to "no rows, no work" instead, which is the semantically
    correct answer for a process that is on its way out.
    """

    __slots__ = ()

    rowcount = -1
    lastrowid = None
    description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, size=None):
        return []

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        return None

    @property
    def row_factory(self):
        return None


class _LockedConnection:
    """Serialises every access to one SQLite connection shared by several threads.

    ``open()`` deliberately uses ``check_same_thread=False`` because the Qt GUI thread
    and the asyncio / yt-dlp / antivirus worker threads all share a single connection.
    Python's sqlite3 module serialises at the C level, but it does *not* serialise the
    Python-level transaction bookkeeping: one thread's ``execute()`` leaves an implicit
    transaction open until its ``commit()``, and a second thread's ``execute()`` during
    that window fails with

        sqlite3.OperationalError: cannot start a transaction within a transaction

    A ``close()`` racing an in-flight write raises the mirror-image
    ``ProgrammingError: Cannot operate on a closed database``. Both are real, observed
    failures, not theoretical ones.

    The lock is reentrant so a single ``Database`` method may freely read and write on
    one thread without deadlocking against itself, and so ``with self._conn:`` still
    works.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn: Optional[sqlite3.Connection] = conn
        self._lock = threading.RLock()
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    def shutdown(self):
        """Close the connection, refusing every later statement."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def row_factory(self):
        if self._closed or self._conn is None:
            return None
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        with self._lock:
            if not self._closed and self._conn is not None:
                self._conn.row_factory = value

    def execute(self, *args, **kwargs):
        with self._lock:
            if self._closed or self._conn is None:
                return _ClosedCursor()
            return _LockedCursor(self._conn.execute(*args, **kwargs), self._lock)

    def executemany(self, *args, **kwargs):
        with self._lock:
            if self._closed or self._conn is None:
                return _ClosedCursor()
            return _LockedCursor(self._conn.executemany(*args, **kwargs), self._lock)

    def executescript(self, *args, **kwargs):
        with self._lock:
            if self._closed or self._conn is None:
                return None
            return self._conn.executescript(*args, **kwargs)

    def commit(self):
        with self._lock:
            if self._closed or self._conn is None:
                return None
            return self._conn.commit()

    def rollback(self):
        with self._lock:
            if self._closed or self._conn is None:
                return None
            return self._conn.rollback()

    def close(self):
        self.shutdown()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc_info):
        try:
            if self._closed or self._conn is None:
                return False
            # Preserve sqlite3's own transaction semantics (commit / rollback).
            return self._conn.__exit__(*exc_info)
        finally:
            self._lock.release()

    def __getattr__(self, name):
        conn = self.__dict__.get("_conn")
        if conn is None or self.__dict__.get("_closed"):
            raise sqlite3.ProgrammingError(
                "Cannot operate on a closed database."
            )
        attr = getattr(conn, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        def guarded(*args, **kwargs):
            with self._lock:
                if self._closed or self._conn is None:
                    raise sqlite3.ProgrammingError(
                        "Cannot operate on a closed database."
                    )
                return attr(*args, **kwargs)

        return guarded


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

#: Fixed id of the queue every download lands in unless the user says otherwise. A constant
#: rather than a generated uuid so the ``queue_id = ''`` backfill is idempotent across launches
#: - a fresh uuid each open would re-home every default download on every start.
DEFAULT_QUEUE_ID = "default"
DEFAULT_QUEUE_NAME = "Default"

#: Sentinel for "show every queue" in the UI and in scope filters. Not a valid queue id.
ALL_QUEUES = ""

#: Queues seeded for the two ingestion paths that produce a recognisable source, so a user's
#: AnimePahe and YouTube downloads are separable without any setup. Fixed ids for the same
#: idempotency reason as DEFAULT_QUEUE_ID. ``source_key`` is matched against a download's
#: metadata by ``DownloadManager._infer_queue_for_source``.
SOURCE_QUEUES = (
    ("queue-animepahe", "AnimePahe", "animepahe"),
    ("queue-youtube", "YouTube", "youtube"),
)

#: Seeded colour per queue, as `#rrggbb`. Distinct hues rather than shades of one, because a
#: queue is identified by colour at a glance in the downloads list and "slightly different
#: grey" is not an identifier. Readable against both light and dark row backgrounds.
DEFAULT_QUEUE_COLOR = "#4a9eff"
SOURCE_QUEUE_COLORS = {
    "queue-animepahe": "#ff7b72",
    "queue-youtube": "#ff5c8a",
}

#: Offered for new queues and by the colour picker. Chosen to stay distinguishable from the two
#: source-queue colours above and to have enough contrast to read as a filled shape against both
#: the light and dark row backgrounds.
QUEUE_COLOR_PALETTE = (
    "#4a9eff",  # blue
    "#3fb950",  # green
    "#d29922",  # amber
    "#a371f7",  # purple
    "#db6d28",  # orange
    "#39c5cf",  # teal
    "#e05561",  # red
    "#8b949e",  # grey
)


def normalize_queue_color(color: str) -> str:
    """Return *color* as a lowercase ``#rrggbb`` string, or ``""`` if it is not a colour.

    Accepts ``#rgb`` and expands it, so a value typed by hand or produced by another tool
    cannot end up as an unparseable value that paints as an invisible swatch. Returns ``""``
    rather than raising, because this runs on values that came from a database which may have
    been written by a future version.
    """
    text = (color or "").strip().lower()
    if not text.startswith("#"):
        text = "#" + text
    digits = text[1:]
    if len(digits) == 3 and all(c in "0123456789abcdef" for c in digits):
        digits = "".join(c * 2 for c in digits)
    if len(digits) != 6 or any(c not in "0123456789abcdef" for c in digits):
        return ""
    return "#" + digits

#: Queues that exist from the start and cannot be deleted. Deleting "YouTube" would silently
#: break the routing below and send every YouTube download to Default instead.
SOURCE_QUEUE_IDS = frozenset(qid for qid, _name, _key in SOURCE_QUEUES)


@dataclass
class QueueInfo:
    """A named download queue, its concurrency budget, and its bandwidth ceilings.

    ``max_concurrent`` is a *local* ceiling; the global ``max_concurrent_downloads`` still
    applies on top. ``<= 0`` means unlimited *within this queue* rather than zero, because a
    queue exists precisely to say "this one is special" - and a user who set it to 0 would be
    expressing the opposite of what they mean.

    ``download_limit`` and ``upload_limit`` are bytes/sec and follow exactly the same rule: ``0``
    adds no ceiling of its own and the global ``NetworkConfig`` limit applies. When both are set
    the *tightest* wins, so a queue can lower the global limit but never raise it.
    """
    id: str = DEFAULT_QUEUE_ID
    name: str = DEFAULT_QUEUE_NAME
    max_concurrent: int = 3
    position: int = 0
    is_default: bool = True
    #: `#rrggbb`, or "" when unset. Drives the swatch in the downloads list, so an unset colour
    #: must degrade to something visible rather than to an invisible cell.
    color: str = DEFAULT_QUEUE_COLOR
    download_limit: int = 0
    upload_limit: int = 0
    created_at: str = ""

    @property
    def effective_max_concurrent(self) -> int:
        """The local ceiling, with ``<= 0`` resolved to unlimited."""
        return self.max_concurrent if self.max_concurrent > 0 else 0


@dataclass
class DownloadEntry:
    """Represents a single download in the database."""
    id: str = ""
    url: str = ""
    filename: str = ""
    save_path: str = ""          # directory
    file_path: str = ""          # full path to file
    total_size: int = 0
    downloaded_size: int = 0
    status: str = "queued"       # queued | downloading | paused | completed | error | seeding | stopped | suspended
    download_type: str = "http"  # http | torrent
    num_segments: int = 8
    error_message: str = ""
    retry_count: int = 0
    max_retries: int = 5
    added_at: str = ""
    last_tried_at: str = ""
    completed_at: str = ""
    etag: str = ""
    content_hash: str = ""
    torrent_info_hash: str = ""
    metadata_json: str = "{}"
    queue_order: int = 0
    # Named queue membership. Distinct from queue_order, which is *priority within* a queue and
    # whose 0 is a meaningful "not in the active queue" sentinel. Empty means the default queue
    # until the backfill runs; every reader normalises through QueueInfo.resolve().
    queue_id: str = ""
    fetching_metadata_since: str = ""  # ISO timestamp when fetching_metadata started
    uploaded_size: int = 0             # Total cumulative seeded/uploaded bytes
    last_seeded_at: str = ""           # ISO timestamp of the most recent completed seed (torrents only)
    seeding_started_at: str = ""       # ISO timestamp the current seeding session began (torrents only)

    # --- UI section header attributes (transient) ---
    is_section_header: bool = False
    section_id: str = ""               # active | seeding | inactive
    section_title: str = ""
    section_count: int = 0
    section_collapsed: bool = False

    # --- transient (not stored in DB) ---
    speed: float = 0.0
    eta_seconds: float = 0.0
    seeds: int = 0
    peers: int = 0
    total_seeds: int = 0
    total_peers: int = 0
    upload_speed: float = 0.0

    @property
    def progress(self) -> float:
        if self.status in ("completed", "seeding"):
            return 100.0
        if self.total_size <= 0:
            return 0.0
        return min(100.0, (self.downloaded_size / self.total_size) * 100.0)

    @property
    def metadata(self) -> dict:
        try:
            data = json.loads(self.metadata_json) if self.metadata_json else {}
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        return _MetadataDict(self, data)

    @metadata.setter
    def metadata(self, value: dict):
        self.metadata_json = json.dumps(
            value if isinstance(value, dict) else {}, default=_json_default
        )


class _MetadataDict(dict):
    """A dict that serialises itself back to ``DownloadEntry.metadata_json`` on mutation.

    Only *top-level* mutations sync. ``DownloadEntry.metadata`` re-parses ``metadata_json``
    on every access and hands back a fresh instance, so this is the only hook that can
    persist a change.

    Consequence, and the reason this is not a deep proxy: a **nested** mutation is silently
    discarded unless the whole container is re-assigned.

    .. code-block:: python

        entry.metadata["manual_seeding"] = True      # persists
        files = entry.metadata["files"]
        files[0]["priority"] = 0                      # lost on its own
        entry.metadata["files"] = files                # this is what persists it

    A deep proxy would remove the footgun but re-wrap the whole structure on every access -
    and ``metadata`` is read several times per second per torrent in ``poll_all`` - so the
    call sites above are written explicitly instead.
    """

    def __init__(self, entry: DownloadEntry, initial: dict):
        super().__init__(initial)
        self._entry = entry

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._sync()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._sync()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._sync()

    def pop(self, *args, **kwargs):
        res = super().pop(*args, **kwargs)
        self._sync()
        return res

    def setdefault(self, key, default=None):
        res = super().setdefault(key, default)
        self._sync()
        return res

    def clear(self):
        super().clear()
        self._sync()

    def _sync(self):
        self._entry.metadata_json = json.dumps(dict(self), default=_json_default)


@dataclass
class SegmentEntry:
    """Represents a single segment of an HTTP segmented download."""
    id: str = ""
    download_id: str = ""
    index: int = 0
    start_byte: int = 0
    end_byte: int = 0
    downloaded_bytes: int = 0
    status: str = "pending"      # pending | downloading | completed | error


# Columns persisted to DB (excludes transient fields)
_DOWNLOAD_DB_COLUMNS = [
    "id", "url", "filename", "save_path", "file_path",
    "total_size", "downloaded_size", "status", "download_type",
    "num_segments", "error_message", "retry_count", "max_retries",
    "added_at", "last_tried_at", "completed_at",
    "etag", "content_hash", "torrent_info_hash", "metadata_json",
    "queue_order", "fetching_metadata_since", "uploaded_size", "last_seeded_at",
    "seeding_started_at", "queue_id",
]

_SEGMENT_DB_COLUMNS = [
    "id", "download_id", "idx", "start_byte", "end_byte",
    "downloaded_bytes", "status",
]


# ---------------------------------------------------------------------------
# Database manager (synchronous — called from background thread)
# ---------------------------------------------------------------------------

class Database:
    """Synchronous SQLite wrapper for download persistence."""

    def __init__(self, db_path: Path | str | None = None):
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle -----------------------------------------------------------

    def open(self):
        raw = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn: Any = _LockedConnection(raw)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def close(self):
        # The shutdown connection object is kept in place rather than replaced with
        # None: a worker thread that is one statement from finishing would otherwise hit
        # `None.commit()` and raise AttributeError from a background thread during
        # teardown. The closed proxy makes every later call inert instead.
        if self._conn is not None:
            self._conn.shutdown()

    @property
    def is_open(self) -> bool:
        """True while the connection is usable."""
        return self._conn is not None and not self._conn.is_closed

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS downloads (
                id              TEXT PRIMARY KEY,
                url             TEXT NOT NULL,
                filename        TEXT NOT NULL DEFAULT '',
                save_path       TEXT NOT NULL DEFAULT '',
                file_path       TEXT NOT NULL DEFAULT '',
                total_size      INTEGER NOT NULL DEFAULT 0,
                downloaded_size INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'queued',
                download_type   TEXT NOT NULL DEFAULT 'http',
                num_segments    INTEGER NOT NULL DEFAULT 8,
                error_message   TEXT NOT NULL DEFAULT '',
                retry_count     INTEGER NOT NULL DEFAULT 0,
                max_retries     INTEGER NOT NULL DEFAULT 5,
                added_at        TEXT NOT NULL DEFAULT '',
                last_tried_at   TEXT NOT NULL DEFAULT '',
                completed_at    TEXT NOT NULL DEFAULT '',
                etag            TEXT NOT NULL DEFAULT '',
                content_hash    TEXT NOT NULL DEFAULT '',
                torrent_info_hash TEXT NOT NULL DEFAULT '',
                metadata_json   TEXT NOT NULL DEFAULT '{}',
                queue_order     INTEGER NOT NULL DEFAULT 0,
                fetching_metadata_since TEXT NOT NULL DEFAULT '',
                uploaded_size   INTEGER NOT NULL DEFAULT 0,
                last_seeded_at  TEXT NOT NULL DEFAULT '',
                seeding_started_at TEXT NOT NULL DEFAULT '',
                queue_id        TEXT NOT NULL DEFAULT ''
            );

CREATE TABLE IF NOT EXISTS segments (
                id              TEXT PRIMARY KEY,
                download_id     TEXT NOT NULL,
                idx             INTEGER NOT NULL,
                start_byte      INTEGER NOT NULL DEFAULT 0,
                end_byte         INTEGER NOT NULL DEFAULT 0,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'pending',
                FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE CASCADE
            );

            -- Declared before `downloads` deliberately, even though `downloads` is created
            -- first above. SQLite only resolves a forward REFERENCES for a DEFERRABLE
            -- constraint, so a non-deferred FK from `downloads` to `queues` cannot be added by
            -- ALTER either. Queue deletion is a manager-level operation that reassigns rows
            -- before deleting, so the FK would buy nothing anyway. See
            -- docs/architecture/queues.md.
            CREATE TABLE IF NOT EXISTS queues (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL UNIQUE,
                max_concurrent  INTEGER NOT NULL DEFAULT 3,
                position        INTEGER NOT NULL DEFAULT 0,
                is_default      INTEGER NOT NULL DEFAULT 0,
                color           TEXT NOT NULL DEFAULT '',
                -- Bandwidth ceilings in bytes/sec. 0 means "no ceiling of its own", which is
                -- deliberately the same word as max_concurrent = 0: the queue adds nothing and
                -- the global NetworkConfig limit applies on top.
                download_limit  INTEGER NOT NULL DEFAULT 0,
                upload_limit    INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_segments_download ON segments(download_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_url ON downloads(url);

            CREATE TABLE IF NOT EXISTS ui_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Bandwidth limits: global (queue_id = '') or per-queue
            CREATE TABLE IF NOT EXISTS bandwidth_limits (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id        TEXT NOT NULL DEFAULT '',
                enabled         INTEGER NOT NULL DEFAULT 1,
                limit_bytes     INTEGER NOT NULL DEFAULT 0,
                limit_type      TEXT NOT NULL DEFAULT 'monthly',  -- 'daily', 'weekly', 'monthly'
                warning_percent INTEGER NOT NULL DEFAULT 80,      -- percentage at which to warn
                created_at      TEXT NOT NULL DEFAULT ''
            );

            -- Bandwidth usage tracking per period
            CREATE TABLE IF NOT EXISTS bandwidth_usage (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id        TEXT NOT NULL DEFAULT '',
                period_start    TEXT NOT NULL,  -- ISO date of period start
                period_type     TEXT NOT NULL,  -- 'daily', 'weekly', 'monthly'
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                uploaded_bytes   INTEGER NOT NULL DEFAULT 0,
                UNIQUE(queue_id, period_start, period_type)
            );

            CREATE INDEX IF NOT EXISTS idx_bandwidth_limits_queue ON bandwidth_limits(queue_id);
            CREATE INDEX IF NOT EXISTS idx_bandwidth_usage_queue_period ON bandwidth_usage(queue_id, period_type, period_start);
        """)

        # Migration check for columns in existing databases
        cursor = self._conn.execute("PRAGMA table_info(downloads)")
        cols = [r["name"] for r in cursor.fetchall()]
        if "queue_order" not in cols:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN queue_order INTEGER NOT NULL DEFAULT 0")
        if "torrent_info_hash" not in cols:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN torrent_info_hash TEXT NOT NULL DEFAULT ''")
        if "metadata_json" not in cols:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        if "fetching_metadata_since" not in cols:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN fetching_metadata_since TEXT NOT NULL DEFAULT ''")
        if "uploaded_size" not in cols:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN uploaded_size INTEGER NOT NULL DEFAULT 0")
        if "last_seeded_at" not in cols:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN last_seeded_at TEXT NOT NULL DEFAULT ''")
        if "seeding_started_at" not in cols:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN seeding_started_at TEXT NOT NULL DEFAULT ''")
        if "queue_id" not in cols:
            # DEFAULT '' rather than the default queue's real id: ALTER TABLE ADD COLUMN can
            # only take a constant, never a subquery, so the backfill below is a second
            # statement. It is idempotent, so running it on every open is harmless.
            self._conn.execute("ALTER TABLE downloads ADD COLUMN queue_id TEXT NOT NULL DEFAULT ''")

        # Same guard for `queues`: CREATE TABLE IF NOT EXISTS will not add a column to a table
        # that already exists, so a database created before queues had a colour needs this.
        cursor = self._conn.execute("PRAGMA table_info(queues)")
        queue_cols = [r["name"] for r in cursor.fetchall()]
        if queue_cols and "color" not in queue_cols:
            self._conn.execute("ALTER TABLE queues ADD COLUMN color TEXT NOT NULL DEFAULT ''")
        # ... and the same for the bandwidth ceilings. Guarded individually because a database
        # can exist from before colours but after nothing else, and `queue_cols` is re-read only
        # once; each ALTER is idempotent and only runs when its column is genuinely absent.
        if queue_cols and "download_limit" not in queue_cols:
            self._conn.execute(
                "ALTER TABLE queues ADD COLUMN download_limit INTEGER NOT NULL DEFAULT 0"
            )
        if queue_cols and "upload_limit" not in queue_cols:
            self._conn.execute(
                "ALTER TABLE queues ADD COLUMN upload_limit INTEGER NOT NULL DEFAULT 0"
            )

        # Create indexes after ensuring columns exist
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_infohash ON downloads(torrent_info_hash)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_queue_order ON downloads(queue_order)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_queue ON downloads(queue_id)")

        self._seed_default_queue()

        # Clean up any orphaned segment rows from deleted downloads
        self._conn.execute("DELETE FROM segments WHERE download_id NOT IN (SELECT id FROM downloads)")

        # Self-heal any completed/seeding downloads whose downloaded_size was zeroed or partial
        self._conn.execute(
            "UPDATE downloads SET downloaded_size = total_size "
            "WHERE status IN ('completed', 'seeding') AND total_size > 0 AND (downloaded_size <= 0 OR downloaded_size < total_size)"
        )

        self._conn.commit()

    # -- queues ---------------------------------------------------------------

    def _seed_default_queue(self):
        """Create the default queue and home any unqueued download in it.

        Idempotent on both halves, so this runs on every open rather than behind a one-shot
        migration flag. The backfill is what makes the ``queue_id = ''`` default invisible: a
        migrated database reads back as though the column had always been there.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO queues (id, name, max_concurrent, position, is_default, color, created_at) "
            "VALUES (?, ?, ?, 0, 1, ?, ?)",
            # max_concurrent 0 = "Global": the default queue adds no cap of its own and follows
            # the global concurrency limit, which is what a user expects of the queue that
            # holds everything unclaimed. Note INSERT OR IGNORE, so an existing profile keeps
            # whatever it already had rather than being reset by this change.
            (DEFAULT_QUEUE_ID, DEFAULT_QUEUE_NAME, 0, DEFAULT_QUEUE_COLOR, _now_iso()),
        )
        # Colours are backfilled separately from the row seed, because an existing profile has
        # a Default queue already and INSERT OR IGNORE would leave it colourless. Only rows with
        # no colour are touched, so a colour the user chose is never overwritten.
        self._conn.execute(
            "UPDATE queues SET color = ? WHERE id = ? AND (color IS NULL OR color = '')",
            (DEFAULT_QUEUE_COLOR, DEFAULT_QUEUE_ID),
        )
        # Also re-assert is_default: a user who deleted every other queue and recreated one by
        # hand should still resolve to a single, unambiguous default.
        self._conn.execute(
            "UPDATE queues SET is_default = 0 WHERE id != ?", (DEFAULT_QUEUE_ID,)
        )
        self._conn.execute(
            "UPDATE downloads SET queue_id = ? WHERE queue_id = '' OR queue_id IS NULL",
            (DEFAULT_QUEUE_ID,),
        )
        # A queue deleted out from under its downloads (e.g. by hand in the DB) would otherwise
        # leave rows pointing at nothing, invisible to any queue-scoped view.
        self._conn.execute(
            "UPDATE downloads SET queue_id = ? WHERE queue_id NOT IN (SELECT id FROM queues)",
            (DEFAULT_QUEUE_ID,),
        )
        # The two source queues are seeded the same idempotent way. max_concurrent 0 = unlimited
        # within the queue: these are for organisation, not for capping, and a user who wants a
        # limit sets one in the queue manager.
        for queue_id, name, _key in SOURCE_QUEUES:
            self._conn.execute(
                "INSERT OR IGNORE INTO queues (id, name, max_concurrent, position, is_default, color, created_at) "
                "VALUES (?, ?, 0, 0, 0, ?, ?)",
                (queue_id, name, SOURCE_QUEUE_COLORS.get(queue_id, ""), _now_iso()),
            )
            self._conn.execute(
                "UPDATE queues SET color = ? WHERE id = ? AND (color IS NULL OR color = '')",
                (SOURCE_QUEUE_COLORS.get(queue_id, ""), queue_id),
            )

    def get_queues(self) -> list[QueueInfo]:
        """All queues, default first then by user position."""
        rows = self._conn.execute(
            "SELECT * FROM queues ORDER BY is_default DESC, position ASC, name COLLATE NOCASE ASC"
        ).fetchall()
        return [self._row_to_queue(r) for r in rows]

    def get_queue(self, queue_id: str) -> Optional[QueueInfo]:
        if not queue_id:
            return self.get_default_queue()
        row = self._conn.execute(
            "SELECT * FROM queues WHERE id = ?", (queue_id,)
        ).fetchone()
        return self._row_to_queue(row) if row else None

    def get_default_queue(self) -> QueueInfo:
        row = self._conn.execute(
            "SELECT * FROM queues WHERE id = ?", (DEFAULT_QUEUE_ID,)
        ).fetchone()
        if row:
            return self._row_to_queue(row)
        # Unreachable via open()/_seed_default_queue, but a caller may hold a Database whose
        # schema was created by a future version; return a usable object rather than None.
        return QueueInfo()

    def get_queue_by_name(self, name: str) -> Optional[QueueInfo]:
        """Find a queue by its display name, case-insensitively.

        Backlog files name queues rather than ids, because a uuid in a hand-editable text file
        would be unusable. Returns ``None`` for an unknown name rather than creating one: a
        backlog can be machine-generated, and auto-creating from generated content is how you
        end up with "Queue1", "Queue2".
        """
        cleaned = (name or "").strip()
        if not cleaned:
            return None
        row = self._conn.execute(
            "SELECT * FROM queues WHERE name = ? COLLATE NOCASE", (cleaned,)
        ).fetchone()
        return self._row_to_queue(row) if row else None

    def create_queue(self, name: str, max_concurrent: int = 3,
                     color: str = "", download_limit: int = 0,
                     upload_limit: int = 0) -> tuple[bool, str]:
        """Create a queue. Returns ``(ok, message)``; a blank or duplicate name is refused.

        A blank *color* is assigned from a small rotating palette so a new queue is visible in
        the downloads list immediately rather than being an invisible swatch the user has to
        think to go and colour. The bandwidth ceilings are bytes/sec, and ``<= 0`` is normalised
        to ``0`` ("no ceiling of its own") rather than stored negative.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            return False, "Queue name cannot be empty."
        existing = self._conn.execute(
            "SELECT id FROM queues WHERE name = ? COLLATE NOCASE", (cleaned,)
        ).fetchone()
        if existing:
            return False, f"A queue named '{cleaned}' already exists."
        row = self._conn.execute(
            "SELECT MAX(position) AS max_pos FROM queues"
        ).fetchone()
        next_pos = (row["max_pos"] + 1) if row and row["max_pos"] is not None else 0
        self._conn.execute(
            "INSERT INTO queues (id, name, max_concurrent, position, is_default, color, "
            "download_limit, upload_limit, created_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), cleaned, int(max_concurrent), next_pos,
                normalize_queue_color(color) or self._next_queue_color(),
                max(0, int(download_limit)), max(0, int(upload_limit)), _now_iso(),
            ),
        )
        self._conn.commit()
        return True, f"Created queue '{cleaned}'."

    def _next_queue_color(self) -> str:
        """The first palette colour not already in use, so a new queue is distinguishable."""
        used = {
            (r["color"] or "").lower()
            for r in self._conn.execute("SELECT color FROM queues").fetchall()
        }
        for candidate in QUEUE_COLOR_PALETTE:
            if candidate.lower() not in used:
                return candidate
        return QUEUE_COLOR_PALETTE[len(used) % len(QUEUE_COLOR_PALETTE)]

    def set_queue_color(self, queue_id: str, color: str) -> tuple[bool, str]:
        """Set a queue's swatch colour. Returns ``(ok, message)``."""
        if not self.get_queue(queue_id):
            return False, "That queue no longer exists."
        cleaned = normalize_queue_color(color)
        if not cleaned:
            return False, "Pick a colour for the queue."
        self._conn.execute(
            "UPDATE queues SET color = ? WHERE id = ?", (cleaned, queue_id)
        )
        self._conn.commit()
        return True, ""

    def rename_queue(self, queue_id: str, name: str) -> tuple[bool, str]:
        """Rename a queue. The default queue cannot be renamed away."""
        cleaned = (name or "").strip()
        if not cleaned:
            return False, "Queue name cannot be empty."
        queue = self.get_queue(queue_id)
        if not queue:
            return False, "That queue no longer exists."
        if queue_id == DEFAULT_QUEUE_ID:
            return False, "The default queue cannot be renamed."
        clash = self._conn.execute(
            "SELECT id FROM queues WHERE name = ? COLLATE NOCASE AND id != ?",
            (cleaned, queue_id),
        ).fetchone()
        if clash:
            return False, f"A queue named '{cleaned}' already exists."
        self._conn.execute(
            "UPDATE queues SET name = ? WHERE id = ?", (cleaned, queue_id)
        )
        self._conn.commit()
        return True, f"Renamed to '{cleaned}'."

    def set_queue_max_concurrent(self, queue_id: str, max_concurrent: int) -> None:
        """Set a queue's local concurrency ceiling. ``<= 0`` means unlimited within the queue."""
        self._conn.execute(
            "UPDATE queues SET max_concurrent = ? WHERE id = ?",
            (int(max_concurrent), queue_id),
        )
        self._conn.commit()

    def set_queue_limits(self, queue_id: str, download_limit: int, upload_limit: int) -> None:
        """Set a queue's bandwidth ceilings in bytes/sec.

        ``<= 0`` in either direction means "no ceiling of its own", so the global limit applies
        on top. Negative input is clamped rather than stored: a queue limit is a ceiling, and a
        negative ceiling would silently read as unlimited while looking like a setting.
        """
        self._conn.execute(
            "UPDATE queues SET download_limit = ?, upload_limit = ? WHERE id = ?",
            (max(0, int(download_limit)), max(0, int(upload_limit)), queue_id),
        )
        self._conn.commit()

    def move_queue_position(self, queue_id: str, delta: int) -> None:
        """Shift a queue up or down in the switcher, keeping `position` dense."""
        queues = [q for q in self.get_queues() if q.id != DEFAULT_QUEUE_ID]
        if not queues:
            return
        idx = next((i for i, q in enumerate(queues) if q.id == queue_id), -1)
        if idx == -1:
            return
        target = idx + (-1 if delta < 0 else 1)
        if target < 0 or target >= len(queues):
            return
        queues[idx], queues[target] = queues[target], queues[idx]
        for position, queue in enumerate(queues):
            self._conn.execute(
                "UPDATE queues SET position = ? WHERE id = ?", (position, queue.id)
            )
        self._conn.commit()

    def delete_queue(self, queue_id: str) -> tuple[bool, str]:
        """Delete a queue, moving its downloads to the default one first.

        Downloads are **never** deleted with their queue: a queue is a view onto history, and
        history is the product. The reassignment and the delete run in one transaction so a
        concurrent insert cannot land a row in the queue between them.
        """
        queue = self.get_queue(queue_id)
        if not queue:
            return False, "That queue no longer exists."
        if queue_id == DEFAULT_QUEUE_ID:
            return False, "The default queue cannot be deleted."
        if queue_id in SOURCE_QUEUE_IDS:
            # These exist to route AnimePahe / YouTube downloads. Deleting one would not break
            # anything, but it would silently send that whole source to Default and look like
            # the routing had stopped working.
            return False, (
                f"'{queue.name}' is a built-in source queue and cannot be deleted. "
                "You can rename it or set its limit."
            )
        with self._conn:
            self._conn.execute(
                "UPDATE downloads SET queue_id = ? WHERE queue_id = ?",
                (DEFAULT_QUEUE_ID, queue_id),
            )
            self._conn.execute("DELETE FROM queues WHERE id = ?", (queue_id,))
        return True, f"Deleted '{queue.name}'; its downloads moved to {DEFAULT_QUEUE_NAME}."

    def reassign_queue(self, download_ids: list[str], queue_id: str) -> int:
        """Move downloads into *queue_id*, continuing the target queue's numbering.

        One transaction, and it does **not** renumber from 1: the target queue already has rows
        numbered 1..n, so restarting would give the incoming rows priorities that collide with
        rows already there. The caller's order is preserved, so a multi-row move produces the
        order the user picked rather than whatever order the rows came back from a SELECT in.

        Returns how many rows changed.
        """
        if not download_ids:
            return 0
        if not self.get_queue(queue_id):
            return 0
        base = self.get_next_queue_order(queue_id)
        with self._conn:
            for offset, download_id in enumerate(download_ids):
                self._conn.execute(
                    "UPDATE downloads SET queue_id = ?, queue_order = ? WHERE id = ?",
                    (queue_id, base + offset, download_id),
                )
        return len(download_ids)

    def get_active_download_ids(self) -> dict[str, str]:
        """Map of download_id -> queue_id for all downloads in an active DB state.

        Active states: 'downloading', 'checking', 'fetching_metadata', 'stalled'.
        Returns a dict mapping download_id to the row's queue_id (defaulting to DEFAULT_QUEUE_ID).
        """
        rows = self._conn.execute(
            "SELECT id, queue_id FROM downloads "
            "WHERE status IN ('downloading','checking','fetching_metadata','stalled')"
        ).fetchall()
        return {r["id"]: (r["queue_id"] or DEFAULT_QUEUE_ID) for r in rows}

    def get_active_counts_by_queue(self) -> dict[str, int]:
        """Active transfers per queue, keyed by queue id.

        One grouped query instead of a row scan per candidate. Queues with nothing active are
        absent from the mapping; callers must treat a missing key as 0 rather than as unknown.
        """
        rows = self._conn.execute(
            "SELECT queue_id, COUNT(*) AS n FROM downloads "
            "WHERE status IN ('downloading','checking','fetching_metadata','stalled') "
            "GROUP BY queue_id"
        ).fetchall()
        return {(r["queue_id"] or DEFAULT_QUEUE_ID): int(r["n"]) for r in rows}

    def get_queue_download_counts(self) -> dict[str, int]:
        """Total rows per queue, for the switcher's per-queue counts."""
        rows = self._conn.execute(
            "SELECT queue_id, COUNT(*) AS n FROM downloads GROUP BY queue_id"
        ).fetchall()
        return {(r["queue_id"] or DEFAULT_QUEUE_ID): int(r["n"]) for r in rows}

    @staticmethod
    def _row_to_queue(row: sqlite3.Row) -> QueueInfo:
        # `color`, `download_limit` and `upload_limit` are read defensively: a row created before
        # a migration, or a future schema that drops one, must not make every queue unreadable.
        try:
            color = row["color"] or ""
        except (IndexError, KeyError):
            color = ""
        try:
            download_limit = int(row["download_limit"] or 0)
        except (IndexError, KeyError):
            download_limit = 0
        try:
            upload_limit = int(row["upload_limit"] or 0)
        except (IndexError, KeyError):
            upload_limit = 0
        return QueueInfo(
            id=row["id"],
            name=row["name"],
            max_concurrent=int(row["max_concurrent"]),
            position=int(row["position"]),
            is_default=bool(row["is_default"]),
            color=color or DEFAULT_QUEUE_COLOR,
            download_limit=download_limit,
            upload_limit=upload_limit,
            created_at=row["created_at"] or "",
        )

    # -- bandwidth limits -------------------------------------------------------

    def get_bandwidth_limits(self, queue_id: str = "") -> list[dict]:
        """Get all bandwidth limits for a queue (or global if queue_id is empty)."""
        rows = self._conn.execute(
            "SELECT * FROM bandwidth_limits WHERE queue_id = ? ORDER BY id",
            (queue_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_bandwidth_limits(self) -> list[dict]:
        """Get all bandwidth limits across all queues."""
        rows = self._conn.execute(
            "SELECT * FROM bandwidth_limits ORDER BY queue_id, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def create_bandwidth_limit(self, queue_id: str, enabled: bool, limit_bytes: int,
                               limit_type: str, warning_percent: int) -> int:
        """Create a new bandwidth limit. Returns the new limit id."""
        cursor = self._conn.execute(
            "INSERT INTO bandwidth_limits (queue_id, enabled, limit_bytes, limit_type, warning_percent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (queue_id, 1 if enabled else 0, limit_bytes, limit_type, warning_percent, _now_iso())
        )
        self._conn.commit()
        return cursor.lastrowid

    def update_bandwidth_limit(self, limit_id: int, enabled: bool, limit_bytes: int,
                               limit_type: str, warning_percent: int) -> bool:
        """Update a bandwidth limit. Returns True if updated."""
        cursor = self._conn.execute(
            "UPDATE bandwidth_limits SET enabled = ?, limit_bytes = ?, limit_type = ?, warning_percent = ? WHERE id = ?",
            (1 if enabled else 0, limit_bytes, limit_type, warning_percent, limit_id)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_bandwidth_limit(self, limit_id: int) -> bool:
        """Delete a bandwidth limit. Returns True if deleted."""
        cursor = self._conn.execute(
            "DELETE FROM bandwidth_limits WHERE id = ?", (limit_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_bandwidth_usage(self, queue_id: str, period_type: str, period_start: str) -> Optional[dict]:
        """Get bandwidth usage for a specific queue, period type, and period start."""
        row = self._conn.execute(
            "SELECT * FROM bandwidth_usage WHERE queue_id = ? AND period_type = ? AND period_start = ?",
            (queue_id, period_type, period_start)
        ).fetchone()
        return dict(row) if row else None

    def add_bandwidth_usage(self, queue_id: str, period_type: str, period_start: str,
                            downloaded_bytes: int = 0, uploaded_bytes: int = 0) -> None:
        """Add or update bandwidth usage for a period."""
        self._conn.execute(
            "INSERT INTO bandwidth_usage (queue_id, period_type, period_start, downloaded_bytes, uploaded_bytes) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(queue_id, period_start, period_type) DO UPDATE SET "
            "downloaded_bytes = downloaded_bytes + excluded.downloaded_bytes, "
            "uploaded_bytes = uploaded_bytes + excluded.uploaded_bytes",
            (queue_id, period_type, period_start, downloaded_bytes, uploaded_bytes)
        )
        self._conn.commit()

    def get_current_period_usage(self, queue_id: str, period_type: str,
                                 now: Optional[Union[datetime, date]] = None) -> tuple[int, int]:
        """Get current period usage (downloaded, uploaded) for a queue (or global if queue_id is empty)."""
        period_start = self._get_period_start(period_type, now)
        if not queue_id or queue_id == "global":
            row = self._conn.execute(
                "SELECT SUM(downloaded_bytes) as dl, SUM(uploaded_bytes) as ul "
                "FROM bandwidth_usage WHERE period_type = ? AND period_start = ?",
                (period_type, period_start)
            ).fetchone()
            if row:
                return (int(row["dl"] or 0), int(row["ul"] or 0))
            return (0, 0)
        else:
            usage = self.get_bandwidth_usage(queue_id, period_type, period_start)
            if usage:
                return (int(usage["downloaded_bytes"] or 0), int(usage["uploaded_bytes"] or 0))
            return (0, 0)

    def _get_period_start(self, period_type: str, now: Optional[Union[datetime, date]] = None) -> str:
        """Get the ISO date string for the start of the current period in local time."""
        if now is None:
            now_dt = datetime.now().astimezone()
        elif isinstance(now, datetime):
            now_dt = now.astimezone() if now.tzinfo else now
        else:
            now_dt = datetime.combine(now, datetime.min.time())

        d = now_dt.date()
        if period_type == "daily":
            return d.isoformat()
        elif period_type == "weekly":
            # Start of week (Monday)
            start = d - timedelta(days=d.weekday())
            return start.isoformat()
        elif period_type == "monthly":
            return d.replace(day=1).isoformat()
        return d.isoformat()

    def check_bandwidth_limit(self, queue_id: str = "", bytes_to_add: int = 0,
                              is_upload: bool = False,
                              now: Optional[Union[datetime, date]] = None) -> tuple[bool, str, float]:
        """Check if adding bytes would exceed any bandwidth limit.

        Enforces global limits first ("global overrides per queue limit"), then
        per-queue limits.
        
        Returns (allowed, message, usage_percentage).
        """
        # 1. Check Global limits first ("global overrides per queue limit")
        global_limits = [
            l for l in (self.get_bandwidth_limits("") + self.get_bandwidth_limits("global"))
            if l["enabled"] and l["limit_bytes"] > 0
        ]
        for limit in global_limits:
            pt = limit["limit_type"]
            dl, ul = self.get_current_period_usage("", pt, now)
            used = dl + ul + bytes_to_add
            limit_bytes = limit["limit_bytes"]
            pct = (used / limit_bytes * 100) if limit_bytes > 0 else 0.0
            if pct >= 100.0:
                return (False, f"Global {pt} bandwidth limit exceeded ({limit_bytes:,} bytes)", 100.0)

        # 2. Check per-queue limits if queue_id specified
        target_qid = queue_id if (queue_id and queue_id != "global") else ""
        if target_qid:
            q_limits = [
                l for l in self.get_bandwidth_limits(target_qid)
                if l["enabled"] and l["limit_bytes"] > 0
            ]
            for limit in q_limits:
                pt = limit["limit_type"]
                dl, ul = self.get_current_period_usage(target_qid, pt, now)
                used = dl + ul + bytes_to_add
                limit_bytes = limit["limit_bytes"]
                pct = (used / limit_bytes * 100) if limit_bytes > 0 else 0.0
                if pct >= 100.0:
                    return (False, f"Queue '{target_qid}' {pt} bandwidth limit exceeded ({limit_bytes:,} bytes)", 100.0)

        # 3. Check for warning thresholds (>= warning_percent and < 100%)
        # Global warnings first
        for limit in global_limits:
            pt = limit["limit_type"]
            dl, ul = self.get_current_period_usage("", pt, now)
            used = dl + ul + bytes_to_add
            limit_bytes = limit["limit_bytes"]
            pct = (used / limit_bytes * 100) if limit_bytes > 0 else 0.0
            if pct >= limit["warning_percent"]:
                return (True, f"Global {pt} bandwidth at {pct:.1f}%", pct)

        # Per-queue warnings next
        if target_qid:
            q_limits = [
                l for l in self.get_bandwidth_limits(target_qid)
                if l["enabled"] and l["limit_bytes"] > 0
            ]
            for limit in q_limits:
                pt = limit["limit_type"]
                dl, ul = self.get_current_period_usage(target_qid, pt, now)
                used = dl + ul + bytes_to_add
                limit_bytes = limit["limit_bytes"]
                pct = (used / limit_bytes * 100) if limit_bytes > 0 else 0.0
                if pct >= limit["warning_percent"]:
                    return (True, f"Queue '{target_qid}' {pt} bandwidth at {pct:.1f}%", pct)

        return (True, "", 0.0)

    def record_bandwidth(self, queue_id: str, downloaded_bytes: int = 0,
                         uploaded_bytes: int = 0,
                         now: Optional[Union[datetime, date]] = None) -> tuple[bool, str, float]:
        """Record bandwidth usage and check limits.
        
        Returns (allowed, warning_message, usage_percentage).
        """
        target_qid = queue_id if (queue_id and queue_id != "global") else "default"
        if downloaded_bytes > 0 or uploaded_bytes > 0:
            for period_type in ("daily", "weekly", "monthly"):
                period_start = self._get_period_start(period_type, now)
                self.add_bandwidth_usage(target_qid, period_type, period_start, downloaded_bytes, uploaded_bytes)

        return self.check_bandwidth_limit(target_qid, 0, False, now)

    # -- downloads -----------------------------------------------------------

    def add_download(self, entry: DownloadEntry) -> DownloadEntry:
        if entry.save_path:
            entry.save_path = normalize_path(entry.save_path)
        if entry.file_path:
            entry.file_path = normalize_path(entry.file_path)
        if not entry.id:
            entry.id = str(uuid.uuid4())
        if not entry.added_at:
            entry.added_at = _now_iso()
        entry.queue_id = self.resolve_queue_id(entry.queue_id)
        if entry.queue_order <= 0:
            entry.queue_order = self.get_next_queue_order(entry.queue_id)

        cols = ", ".join(_DOWNLOAD_DB_COLUMNS)
        placeholders = ", ".join(["?"] * len(_DOWNLOAD_DB_COLUMNS))
        values = [getattr(entry, c) for c in _DOWNLOAD_DB_COLUMNS]

        self._conn.execute(
            f"INSERT INTO downloads ({cols}) VALUES ({placeholders})", values
        )
        self._conn.commit()
        return entry

    def update_download(self, entry: DownloadEntry):
        entry.queue_id = self.resolve_queue_id(entry.queue_id)
        sets = ", ".join(f"{c} = ?" for c in _DOWNLOAD_DB_COLUMNS if c != "id")
        values = [getattr(entry, c) for c in _DOWNLOAD_DB_COLUMNS if c != "id"]
        values.append(entry.id)
        self._conn.execute(
            f"UPDATE downloads SET {sets} WHERE id = ?", values
        )
        self._conn.commit()

    def resolve_queue_id(self, queue_id: str) -> str:
        """Map a possibly-blank or dangling queue id onto one that exists.

        Callers legitimately pass ``""`` to mean "the default queue", so the write paths cannot
        simply reject it. Nor can they blindly write it through: an empty ``queue_id`` would be
        invisible to every queue-scoped read until the next open() re-ran the backfill. This is
        the one place that turns the intent into a real id.
        """
        if not queue_id:
            return DEFAULT_QUEUE_ID
        row = self._conn.execute(
            "SELECT id FROM queues WHERE id = ?", (queue_id,)
        ).fetchone()
        return queue_id if row else DEFAULT_QUEUE_ID

    def update_download_url(self, download_id: str, new_url: str) -> bool:
        """Update a download's URL in the database without altering progress or segments."""
        cursor = self._conn.execute(
            "UPDATE downloads SET url = ? WHERE id = ?",
            (new_url, download_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def update_progress(self, download_id: str, downloaded_size: int,
                        status: str | None = None):
        if status:
            self._conn.execute(
                "UPDATE downloads SET downloaded_size = ?, status = ? WHERE id = ?",
                (downloaded_size, status, download_id),
            )
        else:
            self._conn.execute(
                "UPDATE downloads SET downloaded_size = ? WHERE id = ?",
                (downloaded_size, download_id),
            )
        self._conn.commit()

    def update_status(self, download_id: str, status: str,
                      error_message: Optional[str] = None):
        """Update a download's status.

        ``completed`` and ``downloading`` always clear ``error_message`` - a fresh start
        means the old diagnostic is no longer relevant. ``error`` always records it.

        Every other status leaves it alone *unless* a message is supplied: the default is
        ``None`` meaning "don't touch", so a bare ``update_status(id, "queued")`` cannot
        silently erase a diagnostic, while ``update_status(id, "queued", msg)`` - the
        retry notice - is actually persisted. The same applies to ``file_not_found`` and
        ``threat_detected``, whose messages used to be dropped on the floor.
        """
        now = _now_iso()
        if status == "completed":
            # Only the *first* completion stamps the time. `completed` does double duty:
            # it means both "the payload arrived" and "a seeding session ended", because
            # TorrentEngine.pause() maps a seeder onto it. Re-stamping on every later
            # transition moved the completion date of every torrent whose seeding was
            # stopped by hand, by the ratio limit or the duration limit - which is what
            # "completed today" in the statistics view is built on.
            self._conn.execute(
                "UPDATE downloads SET status = ?, "
                "completed_at = CASE WHEN completed_at = '' THEN ? ELSE completed_at END, "
                "error_message = '' WHERE id = ?",
                (status, now, download_id),
            )
        elif status == "downloading":
            self._conn.execute(
                "UPDATE downloads SET status = ?, last_tried_at = ?, error_message = '' WHERE id = ?",
                (status, now, download_id),
            )
        elif status == "error":
            self._conn.execute(
                "UPDATE downloads SET status = ?, error_message = ? WHERE id = ?",
                (status, error_message or "", download_id),
            )
        elif error_message is not None:
            self._conn.execute(
                "UPDATE downloads SET status = ?, error_message = ? WHERE id = ?",
                (status, error_message, download_id),
            )
        else:
            self._conn.execute(
                "UPDATE downloads SET status = ? WHERE id = ?",
                (status, download_id),
            )
        self._conn.commit()

    def increment_retry(self, download_id: str) -> int:
        self._conn.execute(
            "UPDATE downloads SET retry_count = retry_count + 1, last_tried_at = ? WHERE id = ?",
            (_now_iso(), download_id),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT retry_count FROM downloads WHERE id = ?", (download_id,)
        ).fetchone()
        return row["retry_count"] if row else 0

    def get_download(self, download_id: str) -> Optional[DownloadEntry]:
        row = self._conn.execute(
            "SELECT * FROM downloads WHERE id = ?", (download_id,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_all_downloads(self, queue_id: str = ALL_QUEUES) -> list[DownloadEntry]:
        """Every download, newest first, or only those in one queue.

        The default is *all* queues: most callers want the whole history and history is the
        product. Only the queue-scoped view passes a real id.
        """
        if queue_id:
            rows = self._conn.execute(
                "SELECT * FROM downloads WHERE queue_id = ? ORDER BY added_at DESC, rowid DESC",
                (queue_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM downloads ORDER BY added_at DESC, rowid DESC"
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_completed_downloads(self) -> list[DownloadEntry]:
        """Return all downloads currently in 'completed' status."""
        rows = self._conn.execute(
            "SELECT * FROM downloads WHERE status = 'completed'"
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # -- statistics ---------------------------------------------------------

    #: One bucket per cut-off, in the order the popup shows them, as (name, days back).
    #: 0/6/29/364 gives inclusive 1/7/30/365-day rolling windows - note that "This week"
    #: is a rolling week, not a calendar one.
    _STATS_BUCKETS = (
        ("today", 0),
        ("week", 6),
        ("month", 29),
        ("year", 364),
    )

    #: The calendar-day expression every statistics query groups and compares on.
    #:
    #: ``added_at`` is written by ``_now_iso()`` in **UTC** (``database.py`` top), while the
    #: cut-offs are built from a **local** date, because the buckets are labelled in the
    #: user's own calendar ("Today"). Slicing the raw string therefore compares a UTC date
    #: against a local one and is wrong for part of every day: at 02:00 local in UTC+05:30,
    #: a file added two minutes ago carries ``2026-09-29T20:30`` and lands in *yesterday*.
    #:
    #: ``datetime(added_at, 'localtime')`` re-bases each row into the machine's local zone
    #: before slicing, which is the same conversion ``download_model.get_entry_date_category``
    #: performs with ``astimezone()`` - so the two views of "today" finally agree. SQLite's
    #: ``localtime`` goes through the C library, so it is DST-correct per row rather than
    #: needing an offset plumbed in from Python.
    _STATS_LOCAL_DAY = "substr(datetime(added_at, 'localtime'), 1, 10)"

    #: Month expression, same reasoning, width 7. Also a *group key*, so it must be
    #: written identically in the SELECT list and the WHERE clause.
    _STATS_LOCAL_MONTH = "substr(datetime(added_at, 'localtime'), 1, 7)"

    #: Hour expression, width 13 ('YYYY-MM-DD HH').
    _STATS_LOCAL_HOUR = "substr(datetime(added_at, 'localtime'), 1, 13)"

    #: 5-minute expression, width 16 ('YYYY-MM-DD HH:MM' with minutes quantized to multiples of 5).
    _STATS_LOCAL_5MIN = (
        "substr(datetime(added_at, 'localtime'), 1, 14) || "
        "printf('%02d', (CAST(substr(datetime(added_at, 'localtime'), 15, 2) AS INTEGER) / 5) * 5)"
    )

    #: Minute expression, alias for backward compatibility.
    _STATS_LOCAL_MINUTE = _STATS_LOCAL_5MIN

    #: Excludes rows that cannot be bucketed by date: blank, hand-edited garbage, and
    #: well-shaped but impossible dates like ``2026-13-45`` (which passes the GLOB shape
    #: check but makes ``datetime()`` return NULL). Without the second clause such a row
    #: would produce a NULL group on the chart, surfacing as a literal "None" bucket.
    #: The GLOB is the cheap shape pre-filter; the ``IS NOT NULL`` is the semantic one.
    _STATS_PARSABLE = (
        "added_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
        " AND datetime(added_at, 'localtime') IS NOT NULL"
    )

    def _stats_row_to_bucket(self, row) -> DownloadStats:
        """Aggregate a ``GROUP BY`` row into a bucket, tolerating NULL sums.

        A download with no known size contributes NULL to ``SUM``, and a bare ``None``
        would render as "None GB" in the popup, so every figure goes through int().
        """
        return DownloadStats(
            count=int(row["count"] or 0),
            downloaded=int(row["downloaded"] or 0),
            uploaded=int(row["uploaded"] or 0),
            completed=int(row["completed"] or 0),
        )

    def _stats_sum_for(self, since: Optional[str]) -> DownloadStats:
        """Totals for every row whose local calendar day is on or after *since*.

        *since* is an ISO date prefix (``YYYY-MM-DD``). ``None`` means all time. A row with
        an empty or unparseable ``added_at`` never matches a cut-off, but does count towards
        the lifetime: a hand-written row is still a download the user has.
        """
        completed_sql = (
            f"SUM(CASE WHEN status IN ({','.join('?' * len(COMPLETE_STATUSES))}) "
            f"THEN 1 ELSE 0 END)"
        )
        params: list[Any] = list(COMPLETE_STATUSES)
        where = ""
        if since is not None:
            # Both guards matter. `_STATS_PARSABLE` keeps unparseable rows out of every
            # dated bucket, because a string comparison would otherwise sort them *in*:
            # a hand-edited 'not-a-date' is greater than '2026-09-24' ('n' > '2'), so a
            # corrupt timestamp silently landed in the today bucket. And the comparison has
            # to be on the converted day, not the stored UTC prefix, or "today" is off by a
            # day for everyone east of UTC until lunchtime.
            where = f" WHERE {self._STATS_PARSABLE} AND {self._STATS_LOCAL_DAY} >= ?"
            params.append(since)
        row = self._conn.execute(
            "SELECT COUNT(*) AS count, "
            "       COALESCE(SUM(total_size), 0) AS downloaded, "
            "       COALESCE(SUM(uploaded_size), 0) AS uploaded, "
            f"       {completed_sql} AS completed "
            f"FROM downloads{where}",
            params,
        ).fetchone()
        return self._stats_row_to_bucket(row)

    def get_download_stats(
        self,
        today=None,
        since: Optional[date | datetime | str] = None,
        bucket: str = "day",
        fill_gaps: bool = False,
    ) -> StatsSnapshot:
        """Read every statistics bucket in one call.

        *today* is injected rather than read from the clock, which is what makes the whole
        aggregation testable without freezing time - the same convention the date-based
        segregation uses. It is a **local** date, and so are the buckets the rows are
        grouped into: ``added_at`` is stored in UTC and converted per row (see
        ``_STATS_LOCAL_DAY``). Mixing the two was the off-by-one-day bug this fixes.

        Injecting only the *cut-off* cannot make the SQL itself deterministic across
        machines - the local conversion necessarily reads the host's timezone - so a test
        must compute its expectations with ``datetime.fromisoformat(...).astimezone()``
        rather than hard-coding a UTC date. That keeps the test meaningful on any host
        while still failing if the conversion is removed.

        *since* and *bucket* drive the chart series: the range to plot (None = all time)
        and whether to group by 5-minute intervals, hour, day, or month. The summary buckets
        above are fixed and unaffected - they are the headline numbers, and a chart range
        should not silently redefine them.
        """
        if today is None:
            today = datetime.now().astimezone().date()
        elif isinstance(today, datetime):
            today = today.date()
        if isinstance(since, datetime) and bucket not in ("hour", "5min", "minute"):
            since = since.date()

        values = {}
        for name, days_back in self._STATS_BUCKETS:
            cutoff = (today - timedelta(days=days_back)).isoformat()
            values[name] = self._stats_sum_for(cutoff)
        values["lifetime"] = self._stats_sum_for(None)

        valid_buckets = ("5min", "minute", "hour", "day", "month")
        bucket_val = bucket if bucket in valid_buckets else "day"

        return StatsSnapshot(
            today=values["today"],
            week=values["week"],
            month=values["month"],
            year=values["year"],
            lifetime=values["lifetime"],
            series=self._stats_series(since, bucket_val, fill_gaps=fill_gaps, today=today),
            bucket=bucket_val,
            since=since.isoformat() if hasattr(since, "isoformat") else str(since or ""),
        )

    def _stats_series(
        self,
        since: Optional[date | datetime | str],
        bucket: str,
        fill_gaps: bool = False,
        today: Optional[date | datetime] = None,
    ) -> tuple[tuple[str, DownloadStats], ...]:
        """The chart series, grouped by local 5min, hour, day, or month and clipped to *since*.

        Same ``_STATS_PARSABLE`` guard and same local conversion as the cut-off buckets.
        The group expression is repeated verbatim in the WHERE clause - SQLite will not let
        a WHERE reference a SELECT alias, so the two must be written out identically.
        """
        if bucket in ("5min", "minute"):
            width, day_expr = 16, self._STATS_LOCAL_5MIN
        elif bucket == "hour":
            width, day_expr = 13, self._STATS_LOCAL_HOUR
        elif bucket == "month":
            width, day_expr = 7, self._STATS_LOCAL_MONTH
        else:
            width, day_expr = 10, self._STATS_LOCAL_DAY
        completed_sql = (
            f"SUM(CASE WHEN status IN ({','.join('?' * len(COMPLETE_STATUSES))}) "
            f"THEN 1 ELSE 0 END)"
        )
        params: list[Any] = [*COMPLETE_STATUSES]
        where = f" WHERE {self._STATS_PARSABLE}"
        if since is not None:
            where += f" AND {day_expr} >= ?"
            if isinstance(since, datetime):
                dt = since.astimezone() if since.tzinfo is not None else since
                if bucket in ("5min", "minute"):
                    m = (dt.minute // 5) * 5
                    dt = dt.replace(minute=m, second=0, microsecond=0)
                    since_str = dt.strftime("%Y-%m-%d %H:%M")
                else:
                    since_str = dt.strftime("%Y-%m-%d %H:%M:%S")[:width]
            elif isinstance(since, date):
                if width == 7:
                    since_str = since.strftime("%Y-%m")
                elif width == 13:
                    since_str = since.strftime("%Y-%m-%d 00")
                elif width == 16:
                    since_str = since.strftime("%Y-%m-%d 00:00")
                else:
                    since_str = since.strftime("%Y-%m-%d")
            elif isinstance(since, str):
                since_str = since.replace("T", " ")[:width]
            else:
                since_str = str(since)[:width]
            params.append(since_str)
        rows = self._conn.execute(
            f"SELECT {day_expr} AS bucket, "
            "       COUNT(*) AS count, "
            "       COALESCE(SUM(total_size), 0) AS downloaded, "
            "       COALESCE(SUM(uploaded_size), 0) AS uploaded, "
            f"       {completed_sql} AS completed "
            f"FROM downloads{where} "
            "GROUP BY bucket ORDER BY bucket",
            params,
        ).fetchall()
        raw_series = tuple((str(r["bucket"]), self._stats_row_to_bucket(r)) for r in rows)
        if fill_gaps:
            return fill_series_gaps(raw_series, bucket, start=since, end=today)
        return raw_series

    def get_next_queue_order(self, queue_id: str = "") -> int:
        """Next priority value *within* a queue.

        Scoped because priority is queue-local: a user who puts a torrent at position 1 of its
        own queue must not push everything else in the default queue down by one.
        """
        row = self._conn.execute(
            "SELECT MAX(queue_order) AS max_order FROM downloads "
            "WHERE status IN ('queued', 'downloading', 'checking', 'fetching_metadata', 'stalled', 'paused') "
            "AND queue_order > 0 AND queue_id = ?",
            (queue_id or DEFAULT_QUEUE_ID,),
        ).fetchone()
        if row and row["max_order"] is not None:
            return int(row["max_order"]) + 1
        return 1

    def update_queue_order(self, download_id: str, new_order: int):
        self._conn.execute(
            "UPDATE downloads SET queue_order = ? WHERE id = ?",
            (new_order, download_id),
        )
        self._conn.commit()

    def swap_queue_order(self, id1: str, id2: str):
        entry1 = self.get_download(id1)
        entry2 = self.get_download(id2)
        if entry1 and entry2:
            order1 = entry1.queue_order
            order2 = entry2.queue_order
            self.update_queue_order(id1, order2)
            self.update_queue_order(id2, order1)

    def find_by_url(self, url: str) -> Optional[DownloadEntry]:
        row = self._conn.execute(
            "SELECT * FROM downloads WHERE url = ?", (url,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def find_by_info_hash(self, info_hash: str) -> Optional[DownloadEntry]:
        if not info_hash:
            return None
        row = self._conn.execute(
            "SELECT * FROM downloads WHERE torrent_info_hash = ?", (info_hash,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_recent_save_paths(self, limit: int = 5) -> list[str]:
        """Get the most recent unique save paths ordered by added_at DESC."""
        rows = self._conn.execute(
            """
            SELECT save_path
            FROM (
                SELECT save_path, MAX(added_at) as max_added
                FROM downloads
                WHERE save_path IS NOT NULL AND save_path != ''
                GROUP BY save_path
            )
            ORDER BY max_added DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
        return [normalize_path(r["save_path"]) for r in rows if r["save_path"]]

    def delete_download(self, download_id: str):
        self._conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
        self._conn.commit()

    def move_download(self, download_id: str, new_save_path: str,
                      new_file_path: str):
        self._conn.execute(
            "UPDATE downloads SET save_path = ?, file_path = ? WHERE id = ?",
            (new_save_path, new_file_path, download_id),
        )
        self._conn.commit()

    # -- segments ------------------------------------------------------------

    def add_segments(self, segments: list[SegmentEntry]):
        for seg in segments:
            if not seg.id:
                seg.id = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO segments (id, download_id, idx, start_byte, end_byte, downloaded_bytes, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seg.id, seg.download_id, seg.index, seg.start_byte,
                 seg.end_byte, seg.downloaded_bytes, seg.status),
            )
        self._conn.commit()

    def get_segments(self, download_id: str) -> list[SegmentEntry]:
        rows = self._conn.execute(
            "SELECT * FROM segments WHERE download_id = ? ORDER BY idx",
            (download_id,),
        ).fetchall()
        return [self._row_to_segment(r) for r in rows]

    def update_segment(self, segment_id: str, downloaded_bytes: int,
                       status: str):
        self._conn.execute(
            "UPDATE segments SET downloaded_bytes = ?, status = ? WHERE id = ?",
            (downloaded_bytes, status, segment_id),
        )
        self._conn.commit()

    def delete_segments(self, download_id: str):
        self._conn.execute(
            "DELETE FROM segments WHERE download_id = ?", (download_id,)
        )
        self._conn.commit()

    # -- UI & Window state ---------------------------------------------------

    def set_ui_state(self, key: str, value: Any):
        """Store a JSON-serializable UI state value."""
        val_str = json.dumps(value)
        with self._conn:
            self._conn.execute(
                "INSERT INTO ui_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, val_str)
            )

    def get_ui_state(self, key: str, default: Any = None) -> Any:
        """Retrieve a JSON-serialized UI state value."""
        row = self._conn.execute(
            "SELECT value FROM ui_state WHERE key = ?", (key,)
        ).fetchone()
        if row and row["value"]:
            try:
                return json.loads(row["value"])
            except Exception:
                return default
        return default

    def save_window_state(self, state: dict):
        """Store entire window geometry, location, maximized state, column lengths, etc."""
        self.set_ui_state("window_state", state)

    def get_window_state(self) -> dict:
        """Retrieve stored window geometry, location, maximized state, column lengths, etc."""
        return self.get_ui_state("window_state", default={})

    def save_preferences_window_size(self, width: int, height: int):
        """Store preferences dialog window dimensions."""
        self.set_ui_state("preferences_dialog_size", {"width": int(width), "height": int(height)})

    def get_preferences_window_size(self) -> dict:
        """Retrieve stored preferences dialog window dimensions."""
        return self.get_ui_state("preferences_dialog_size", default={})

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> DownloadEntry:
        d = {k: row[k] for k in _DOWNLOAD_DB_COLUMNS}
        if d.get("save_path"):
            d["save_path"] = normalize_path(d["save_path"])
        if d.get("file_path"):
            d["file_path"] = normalize_path(d["file_path"])
        entry = DownloadEntry(**d)
        if entry.metadata:
            meta = entry.metadata
            if "seeds" in meta:
                entry.seeds = to_int(meta["seeds"])
            if "peers" in meta:
                entry.peers = to_int(meta["peers"])
            if "total_seeds" in meta:
                entry.total_seeds = to_int(meta["total_seeds"])
            if "total_peers" in meta:
                entry.total_peers = to_int(meta["total_peers"])
            if not entry.uploaded_size and "total_seeded_bytes" in meta:
                try:
                    entry.uploaded_size = int(meta["total_seeded_bytes"])
                except (ValueError, TypeError):
                    pass
        if entry.status in ("completed", "seeding"):
            if entry.total_size > 0 and entry.downloaded_size < entry.total_size:
                entry.downloaded_size = entry.total_size
            elif entry.total_size <= 0 and entry.downloaded_size > 0:
                entry.total_size = entry.downloaded_size
            elif entry.total_size <= 0 and entry.file_path:
                try:
                    fp = Path(entry.file_path)
                    if fp.exists() and fp.is_file():
                        st = fp.stat().st_size
                        if st > 0:
                            entry.total_size = st
                            entry.downloaded_size = st
                except Exception:
                    pass
        return entry

    @staticmethod
    def _row_to_segment(row: sqlite3.Row) -> SegmentEntry:
        return SegmentEntry(
            id=row["id"],
            download_id=row["download_id"],
            index=row["idx"],
            start_byte=row["start_byte"],
            end_byte=row["end_byte"],
            downloaded_bytes=row["downloaded_bytes"],
            status=row["status"],
        )
