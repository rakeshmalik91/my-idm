"""SQLite database layer for download history and state persistence."""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from my_idm.utils import normalize_path, to_int


APP_DIR = Path.home() / ".my-idm"
DB_PATH = APP_DIR / "downloads.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """A dictionary wrapper that automatically serializes back to DownloadEntry.metadata_json upon mutation."""

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
    "seeding_started_at",
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
                seeding_started_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS segments (
                id              TEXT PRIMARY KEY,
                download_id     TEXT NOT NULL,
                idx             INTEGER NOT NULL,
                start_byte      INTEGER NOT NULL DEFAULT 0,
                end_byte        INTEGER NOT NULL DEFAULT 0,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'pending',
                FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_segments_download ON segments(download_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_url ON downloads(url);

            CREATE TABLE IF NOT EXISTS ui_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
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

        # Create indexes after ensuring columns exist
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_infohash ON downloads(torrent_info_hash)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_queue_order ON downloads(queue_order)")

        # Clean up any orphaned segment rows from deleted downloads
        self._conn.execute("DELETE FROM segments WHERE download_id NOT IN (SELECT id FROM downloads)")

        # Self-heal any completed/seeding downloads whose downloaded_size was zeroed or partial
        self._conn.execute(
            "UPDATE downloads SET downloaded_size = total_size "
            "WHERE status IN ('completed', 'seeding') AND total_size > 0 AND (downloaded_size <= 0 OR downloaded_size < total_size)"
        )

        self._conn.commit()

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
        if entry.queue_order <= 0:
            entry.queue_order = self.get_next_queue_order()

        cols = ", ".join(_DOWNLOAD_DB_COLUMNS)
        placeholders = ", ".join(["?"] * len(_DOWNLOAD_DB_COLUMNS))
        values = [getattr(entry, c) for c in _DOWNLOAD_DB_COLUMNS]

        self._conn.execute(
            f"INSERT INTO downloads ({cols}) VALUES ({placeholders})", values
        )
        self._conn.commit()
        return entry

    def update_download(self, entry: DownloadEntry):
        sets = ", ".join(f"{c} = ?" for c in _DOWNLOAD_DB_COLUMNS if c != "id")
        values = [getattr(entry, c) for c in _DOWNLOAD_DB_COLUMNS if c != "id"]
        values.append(entry.id)
        self._conn.execute(
            f"UPDATE downloads SET {sets} WHERE id = ?", values
        )
        self._conn.commit()

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
                      error_message: str = ""):
        now = _now_iso()
        if status == "completed":
            self._conn.execute(
                "UPDATE downloads SET status = ?, completed_at = ?, error_message = '' WHERE id = ?",
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

    def get_all_downloads(self) -> list[DownloadEntry]:
        rows = self._conn.execute(
            "SELECT * FROM downloads ORDER BY added_at DESC"
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_next_queue_order(self) -> int:
        row = self._conn.execute(
            "SELECT MAX(queue_order) AS max_order FROM downloads "
            "WHERE status IN ('queued', 'downloading', 'checking', 'fetching_metadata', 'stalled') "
            "AND queue_order > 0"
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
