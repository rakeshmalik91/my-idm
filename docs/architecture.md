# Architecture Guide

Technical documentation for developers working on the My-IDM codebase.

---

## Table of Contents

- [Overview](#overview)
- [Module Map](#module-map)
- [Threading Model](#threading-model)
- [Data Flow](#data-flow)
- [Database Schema](#database-schema)
- [HTTP Engine Internals](#http-engine-internals)
- [Torrent Engine Internals](#torrent-engine-internals)
- [GUI Architecture](#gui-architecture)
- [Key Design Decisions](#key-design-decisions)
- [Adding New Features](#adding-new-features)

---

## Overview

My-IDM follows a layered architecture with clear separation of concerns:

```
┌─────────────────────────────────────────────────┐
│                    GUI Layer                     │
│  MainWindow → DownloadTableModel → Delegates    │
│                    Dialogs                       │
├─────────────────────────────────────────────────┤
│               Manager Layer                      │
│         DownloadManager (QObject)                │
│   Orchestrates engines, DB, and GUI signals      │
├─────────────────────────────────────────────────┤
│               Engine Layer                       │
│     HTTPEngine          TorrentEngine            │
│   (asyncio/aiohttp)     (libtorrent)             │
├─────────────────────────────────────────────────┤
│               Data Layer                         │
│          Database (SQLite)                       │
│    DownloadEntry    SegmentEntry                 │
└─────────────────────────────────────────────────┘
```

---

## Module Map

```
my_idm/
├── __init__.py          # Package metadata, version string
├── main.py              # Entry point, CLI arg parsing, app setup
├── database.py          # SQLite persistence, data classes
├── http_engine.py       # Segmented HTTP downloads with aiohttp
├── torrent_engine.py    # libtorrent wrapper for torrents
├── manager.py           # Central orchestrator (QObject with signals)
├── main_window.py       # QMainWindow with toolbar, menus, table
├── download_model.py    # QAbstractTableModel (12 columns)
├── delegates.py         # ProgressBarDelegate for QTableView
├── dialogs.py           # AddDownloadDialog, MoveDialog, DeleteDialog
└── styles.py            # Dark theme QSS stylesheet, color palette
```

### Dependency Graph

```
main.py
  ├── database.py
  ├── manager.py
  │     ├── database.py
  │     ├── http_engine.py
  │     │     └── database.py
  │     └── torrent_engine.py
  │           └── database.py
  ├── main_window.py
  │     ├── manager.py
  │     ├── download_model.py
  │     │     ├── database.py (DownloadEntry)
  │     │     └── styles.py (Colors)
  │     ├── delegates.py
  │     │     └── styles.py (Colors)
  │     └── dialogs.py
  └── styles.py
```

---

## Threading Model

My-IDM uses three execution contexts:

### 1. Qt Main Thread

- All GUI operations (widget updates, signal handling, user interaction)
- The `QApplication` event loop runs here
- `DownloadManager` lives here as a `QObject`
- Torrent polling timer (`_torrent_timer`, 1-second interval) runs here
- Retry timer (`_retry_timer`, 10-second interval) runs here

### 2. asyncio Background Thread

- A dedicated `threading.Thread` named `"idm-async"` runs an `asyncio` event loop
- All `aiohttp` I/O happens on this loop
- `HTTPEngine` coroutines run here
- Communication with the main thread:
  - **Main → Async**: `asyncio.run_coroutine_threadsafe(coro, loop)` to schedule downloads
  - **Async → Main**: Engine callbacks are invoked from the async thread; they call `DownloadManager` methods that emit Qt signals (which are cross-thread safe)

### 3. SQLite Thread Safety

- SQLite is opened with `check_same_thread=False` and `journal_mode=WAL`
- WAL mode allows concurrent reads with a single writer
- The `Database` class is accessed from both the main thread and the async thread
- Individual operations are atomic and committed immediately

```
┌──────────────────┐   signals    ┌──────────────────┐
│   Qt Main Thread │ ◄─────────── │  asyncio Thread  │
│                  │              │                  │
│  MainWindow      │  run_coro   │  HTTPEngine      │
│  DownloadManager │ ──────────► │  aiohttp tasks   │
│  TorrentEngine   │              │                  │
│  (polling timer) │              │                  │
└──────────────────┘              └──────────────────┘
         │                                │
         │        ┌──────────┐            │
         └───────►│  SQLite  │◄───────────┘
                  │   (WAL)  │
                  └──────────┘
```

---

## Data Flow

### Adding a Download

```
User clicks "Add URL"
  → AddDownloadDialog shown
  → User enters URL and clicks "Download"
  → MainWindow._on_add()
  → DownloadManager.add_download(url, save_path, segments)
    → Deduplication check: Database.find_by_url(url)
    → If duplicate and incomplete: resume existing entry
    → If new: Database.add_download(entry)
    → Emit download_added signal
    → _start_entry(entry):
        → If HTTP: asyncio.run_coroutine_threadsafe(http.add(entry))
        → If Torrent: torrent.add_torrent(entry)
  ← MainWindow._on_download_added(download_id)
    → DownloadTableModel.add_entry(entry)
```

### Progress Updates

```
# HTTP path:
aiohttp downloads chunk
  → HTTPEngine._emit_progress(id, downloaded, total, speed, eta)
  → DownloadManager._on_http_progress(...)  [callback, async thread]
  → Emit progress_updated signal           [cross-thread Qt signal]
  → MainWindow._on_progress_updated(...)   [main thread slot]
  → DownloadTableModel.update_progress(...)
  → dataChanged signal → QTableView repaints

# Torrent path:
QTimer fires every 1 second
  → DownloadManager._poll_torrents()
  → TorrentEngine.poll_all()
    → For each handle: handle.status()
    → TorrentEngine._progress_cb(...)
    → DownloadManager._on_torrent_progress(...)
    → Emit progress_updated signal
    → [same as HTTP from here]
```

### Automatic Retry Flow

```
Download fails with exception
  → HTTPEngine._run_download() catches it
  → HTTPEngine._handle_retry(entry, error_msg)
    → Database.increment_retry(id)
    → If retries < max_retries:
        → Database.update_status(id, "queued")
        → Emit status "queued"
    → Else:
        → Database.update_status(id, "error")
        → Emit status "error"

Every 10 seconds:
  → DownloadManager._process_retry_queue()
  → For each entry where status == "queued" and retry_count > 0:
    → _start_entry(entry)  [re-dispatch to engine]
```

---

## Database Schema

### `downloads` Table

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | TEXT PK | UUID | Unique download identifier |
| `url` | TEXT | — | Source URL or magnet link |
| `filename` | TEXT | `''` | Resolved filename |
| `save_path` | TEXT | `''` | Parent directory |
| `file_path` | TEXT | `''` | Full path to file on disk |
| `total_size` | INTEGER | `0` | Expected file size in bytes |
| `downloaded_size` | INTEGER | `0` | Bytes downloaded so far |
| `status` | TEXT | `'queued'` | Current state |
| `download_type` | TEXT | `'http'` | `http` or `torrent` |
| `num_segments` | INTEGER | `8` | Parallel connections (HTTP) |
| `error_message` | TEXT | `''` | Last error description |
| `retry_count` | INTEGER | `0` | Retry attempts so far |
| `max_retries` | INTEGER | `5` | Maximum retry attempts |
| `added_at` | TEXT | `''` | ISO 8601 timestamp |
| `last_tried_at` | TEXT | `''` | ISO 8601 timestamp |
| `completed_at` | TEXT | `''` | ISO 8601 timestamp |
| `etag` | TEXT | `''` | HTTP ETag for resume validation |
| `content_hash` | TEXT | `''` | File hash for integrity |
| `torrent_info_hash` | TEXT | `''` | BitTorrent info-hash |
| `metadata_json` | TEXT | `'{}'` | Extensible JSON blob |

### `segments` Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Segment identifier |
| `download_id` | TEXT FK | References `downloads.id` |
| `idx` | INTEGER | Segment index (0-based) |
| `start_byte` | INTEGER | First byte of this segment |
| `end_byte` | INTEGER | Last byte of this segment |
| `downloaded_bytes` | INTEGER | Bytes written so far |
| `status` | TEXT | `pending`, `downloading`, `completed`, `error` |

### Indexes

- `idx_segments_download` on `segments(download_id)` — fast segment lookups
- `idx_downloads_url` on `downloads(url)` — deduplication by URL
- `idx_downloads_infohash` on `downloads(torrent_info_hash)` — deduplication by info-hash

### Data Classes

```python
@dataclass
class DownloadEntry:
    # Persisted fields (20 columns)
    id, url, filename, save_path, file_path, total_size,
    downloaded_size, status, download_type, num_segments,
    error_message, retry_count, max_retries, added_at,
    last_tried_at, completed_at, etag, content_hash,
    torrent_info_hash, metadata_json

    # Transient fields (not stored in DB, used by GUI)
    speed, eta_seconds, seeds, peers, upload_speed

    # Computed properties
    progress → float (0-100%)
    metadata → dict (parsed from metadata_json)

@dataclass
class SegmentEntry:
    id, download_id, index, start_byte, end_byte,
    downloaded_bytes, status
```

---

## HTTP Engine Internals

### Segmented Download Flow

```
_run_download(entry)
  │
  ├── _probe_url(url)           # HEAD request
  │     └── Returns: supports_range, total_size, etag, filename
  │
  ├── If supports_range AND total_size > 256 KiB:
  │     └── _segmented_download(entry, cancel_evt)
  │           ├── Load existing segments from DB (for resume)
  │           │   OR create new segments + pre-allocate file
  │           ├── For each incomplete segment:
  │           │     └── _download_one_segment(entry, seg, ...)
  │           │           ├── Range: bytes={start+downloaded}-{end}
  │           │           ├── Write chunks via f.seek(offset)
  │           │           ├── Update progress aggregate
  │           │           └── Retry loop (5 attempts, exp backoff)
  │           └── If _FallbackToSingle raised:
  │                 └── Delete segments, reset progress
  │
  └── Else (or on fallback):
        └── _single_download(entry, cancel_evt)
              ├── Resume from existing file size
              ├── Range: bytes={existing_size}-
              └── Append to file (or overwrite if server ignores range)
```

### Key Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `DEFAULT_SEGMENTS` | 8 | Number of parallel connections |
| `CHUNK_SIZE` | 64 KiB | Read buffer per iteration |
| `MAX_RETRIES_PER_SEGMENT` | 5 | Retry limit per segment |
| `RETRY_BASE_DELAY` | 1.0s | Exponential backoff base |
| `CONNECT_TIMEOUT` | 30s | TCP connection timeout |
| `READ_TIMEOUT` | 60s | Socket read timeout |

### Fallback Triggers

The segmented download falls back to single-stream when:
- `HEAD` response doesn't include `Accept-Ranges: bytes`
- Content-Length is unknown or too small (< 256 KiB)
- Any segment receives HTTP 416 (Range Not Satisfiable)
- Any segment receives HTTP 403 (Forbidden)
- Server returns 200 instead of 206 on a range request (segment index > 0)

### Pause/Resume Mechanism

**Pause:**
1. `cancel_evt.set()` — signals all active coroutines
2. Each segment saves its `downloaded_bytes` to the `segments` table
3. The active `aiohttp` task awaits completion (5s timeout, then cancel)
4. Status set to `"paused"`

**Resume:**
1. Load segments from DB with their saved offsets
2. Skip segments where `status == "completed"`
3. For each remaining segment, request `Range: bytes={start + downloaded_bytes}-{end}`
4. Continue writing at the correct file offset

---

## Torrent Engine Internals

### Session Lifecycle

```
start():
  lt.session_params() + lt.default_settings()
  → Set alert_mask (status | error | storage)
  → Create lt.session(params)

stop():
  → For each handle: save_resume_data()
  → Delete session
```

### Torrent Handle Management

| Operation | libtorrent Call |
|-----------|----------------|
| Add magnet | `lt.parse_magnet_uri(url)` → `session.add_torrent(params)` |
| Add .torrent | `lt.torrent_info(path)` → `session.add_torrent(params)` |
| Pause | `handle.pause()` |
| Resume | `handle.resume()` |
| Recheck | `handle.force_recheck()` |
| Move | `handle.move_storage(new_path)` |
| Remove | `session.remove_torrent(handle, [delete_files])` |

### Progress Polling

A `QTimer` fires every 1 second in the main thread:

```python
def poll_all():
    for download_id, handle in self._handles.items():
        s = handle.status()
        → total_size = s.total_wanted
        → downloaded = s.total_wanted_done
        → speed = s.download_rate
        → seeds = s.num_seeds
        → peers = s.num_peers
        → Emit progress callback
        → Check for completion
```

### Fast Resume

- On pause/stop: `handle.save_resume_data()` → save to `~/.my-idm/fastresume/{id}.fastresume`
- On start: if file exists, load resume data into `add_torrent_params.resume_data`

---

## GUI Architecture

### Widget Hierarchy

```
QMainWindow (MainWindow)
  ├── QMenuBar
  │     ├── File (Add URL, Add Torrent, Load Backlog, Exit)
  │     ├── Edit (Pause, Resume, Delete, Move, Recheck)
  │     ├── View (Select All)
  │     └── Help (About)
  ├── QToolBar
  │     └── [Add URL][Add Torrent] | [Pause][Resume] | [Delete][Move][Recheck] | [Open File][Open Folder]
  ├── QTableView (central widget)
  │     ├── Model: DownloadTableModel
  │     └── Delegate: ProgressBarDelegate (column 2)
  └── QStatusBar
        ├── Status label
        ├── Speed label
        └── Count label
```

### Model-View Architecture

```
DownloadTableModel (QAbstractTableModel)
  │
  ├── _entries: list[DownloadEntry]     # In-memory data
  ├── _id_to_row: dict[str, int]        # Fast lookup
  │
  ├── load_entries([entries])            # Full reset (startup)
  ├── add_entry(entry)                   # Insert row
  ├── remove_entry(download_id)          # Remove row
  ├── update_progress(id, ...)           # Efficient partial update
  ├── update_status(id, status)          # Status + color update
  └── refresh_entry(id, entry)           # Full row refresh
```

### Column Definitions

Defined in the `Col` class as integer constants:

```python
class Col:
    NAME = 0         # Filename or URL[:60]
    SIZE = 1         # humanize.naturalsize(total_size)
    PROGRESS = 2     # dict → ProgressBarDelegate
    STATUS = 3       # Capitalized status with color
    SPEED = 4        # Speed or upload speed
    ETA = 5          # Estimated time remaining
    TYPE = 6         # "HTTP" or "TORRENT"
    SEEDS_PEERS = 7  # "S:5 P:12" or "8 seg"
    ADDED = 8        # ISO → "YYYY-MM-DD HH:MM"
    LAST_TRIED = 9   # ISO → "YYYY-MM-DD HH:MM"
    COMPLETED = 10   # ISO → "YYYY-MM-DD HH:MM"
    SAVE_PATH = 11   # Directory path
```

### ProgressBarDelegate

The progress column uses a custom `QStyledItemDelegate` that:

1. Receives a `dict` with `{"progress": float, "status": str}` from the model
2. Draws a rounded rectangle background track
3. Fills a proportional rounded rectangle with a gradient based on status color
4. Overlays the percentage text in bold

Colors per status:
- Downloading → `#1f6feb` (blue)
- Completed → `#238636` (green)
- Paused → `#9e6a03` (amber)
- Error → `#da3633` (red)
- Seeding → `#8957e5` (purple)

### Signal Flow

```
DownloadManager signals:
  progress_updated(str, int, int, float, float, int, int, float)
    → MainWindow._on_progress_updated → model.update_progress

  status_changed(str, str, str)
    → MainWindow._on_status_changed → model.update_status

  download_added(str)
    → MainWindow._on_download_added → model.add_entry

  download_removed(str)
    → MainWindow._on_download_removed → model.remove_entry

  download_moved(str)
    → MainWindow._on_download_moved → model.refresh_entry
```

---

## Key Design Decisions

### Why asyncio in a separate thread?

Qt has its own event loop. Rather than using `qasync` (which can have compatibility issues), we run a vanilla `asyncio` loop in a daemon thread. Communication is achieved via:
- `asyncio.run_coroutine_threadsafe()` for main → async
- Qt signals (thread-safe) for async → main

### Why synchronous SQLite instead of aiosqlite?

The original plan included `aiosqlite`, but synchronous SQLite with `check_same_thread=False` is simpler and sufficient:
- Individual DB operations are fast (< 1ms)
- WAL mode prevents write locks from blocking reads
- The complexity of async DB access isn't justified for this workload

### Why callbacks instead of signals in engines?

The HTTP and Torrent engines use plain Python callbacks instead of Qt signals because:
- They run in non-Qt threads (asyncio loop)
- The manager translates callbacks → Qt signals for thread safety
- This keeps the engines decoupled from Qt

### Why pre-allocate files for segmented downloads?

The file is `truncate()`d to its full size before downloading begins. This allows multiple segments to `seek()` and write to their assigned offsets concurrently without conflicts, using `open(file, "r+b")`.

---

## Adding New Features

### Adding a New Column

1. Add a constant to `Col` in `download_model.py` and update `HEADERS` list
2. Add display logic in `DownloadTableModel._display_data()`
3. Set column width in `MainWindow._setup_ui()`
4. If needed, add a field to `DownloadEntry` in `database.py`

### Adding a New Download Engine

1. Create `my_idm/new_engine.py` with `start()`, `stop()`, `add()`, `pause()`, `resume()`, `cancel()` methods
2. Accept a `Database` instance and progress/status callbacks
3. Register it in `DownloadManager.__init__()`
4. Update `_detect_type()` and `_start_entry()` in `manager.py`
5. Add polling if the engine doesn't push updates

### Adding a New Action

1. Create a `QAction` in `MainWindow._setup_actions()`
2. Add it to the toolbar in `_setup_toolbar()` and/or menu in `_setup_menubar()`
3. Add it to the context menu in `_show_context_menu()`
4. Implement the handler method (e.g., `_on_new_action()`)
5. If it needs manager support, add a method to `DownloadManager`
