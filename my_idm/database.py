"""SQLite database layer for download history and state persistence."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


APP_DIR = Path.home() / ".my-idm"
DB_PATH = APP_DIR / "downloads.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    status: str = "queued"       # queued | downloading | paused | completed | error | seeding
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

    # --- transient (not stored in DB) ---
    speed: float = 0.0
    eta_seconds: float = 0.0
    seeds: int = 0
    peers: int = 0
    upload_speed: float = 0.0

    @property
    def progress(self) -> float:
        if self.total_size <= 0:
            return 0.0
        return min(100.0, (self.downloaded_size / self.total_size) * 100.0)

    @property
    def metadata(self) -> dict:
        try:
            return json.loads(self.metadata_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    @metadata.setter
    def metadata(self, value: dict):
        self.metadata_json = json.dumps(value)


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
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

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
                metadata_json   TEXT NOT NULL DEFAULT '{}'
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
            CREATE INDEX IF NOT EXISTS idx_downloads_infohash ON downloads(torrent_info_hash);
        """)
        self._conn.commit()

    # -- downloads -----------------------------------------------------------

    def add_download(self, entry: DownloadEntry) -> DownloadEntry:
        if not entry.id:
            entry.id = str(uuid.uuid4())
        if not entry.added_at:
            entry.added_at = _now_iso()

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

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> DownloadEntry:
        return DownloadEntry(**{
            k: row[k] for k in _DOWNLOAD_DB_COLUMNS
        })

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
