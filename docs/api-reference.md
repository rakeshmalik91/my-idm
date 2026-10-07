# API Reference

Complete reference for all public classes, methods, and data types in My-IDM.

---

## Table of Contents

- [my_idm.database](#my_idmdatabase)
- [my_idm.http_engine](#my_idmhttp_engine)
- [my_idm.torrent_engine](#my_idmtorrent_engine)
- [my_idm.manager](#my_idmmanager)
- [my_idm.download_model](#my_idmdownload_model)
- [my_idm.delegates](#my_idmdelegates)
- [my_idm.dialogs](#my_idmdialogs)
- [my_idm.styles](#my_idmstyles)

---

## `my_idm.database`

### Constants

| Name | Type | Value | Description |
|------|------|-------|-------------|
| `APP_DIR` | `Path` | `~/.my-idm` | Application data directory |
| `DB_PATH` | `Path` | `~/.my-idm/downloads.db` | Default database path |

---

### `DownloadEntry`

```python
@dataclass
class DownloadEntry
```

Represents a single download in the database.

#### Persisted Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | `""` | UUID primary key |
| `url` | `str` | `""` | Source URL, magnet link, or .torrent file path |
| `filename` | `str` | `""` | Resolved filename |
| `save_path` | `str` | `""` | Parent directory path |
| `file_path` | `str` | `""` | Full absolute path to file |
| `total_size` | `int` | `0` | Expected total size in bytes |
| `downloaded_size` | `int` | `0` | Bytes downloaded so far |
| `status` | `str` | `"queued"` | One of: `queued`, `downloading`, `paused`, `stopped`, `completed`, `seeding`, `checking`, `fetching_metadata`, `stalled`, `suspended`, `scanning`, `threat_detected`, `file_not_found`, `error` |
| `download_type` | `str` | `"http"` | One of: `http`, `torrent` |
| `num_segments` | `int` | `8` | Number of parallel connections (HTTP only) |
| `error_message` | `str` | `""` | Last error description |
| `retry_count` | `int` | `0` | Number of retry attempts so far |
| `max_retries` | `int` | `5` | Maximum retry attempts before marking as error |
| `added_at` | `str` | `""` | ISO 8601 UTC timestamp when first added |
| `last_tried_at` | `str` | `""` | ISO 8601 UTC timestamp of last download attempt |
| `completed_at` | `str` | `""` | ISO 8601 UTC timestamp of completion |
| `etag` | `str` | `""` | HTTP ETag header for resume validation |
| `content_hash` | `str` | `""` | File hash for integrity verification |
| `torrent_info_hash` | `str` | `""` | BitTorrent info-hash for deduplication |
| `metadata_json` | `str` | `"{}"` | Extensible JSON blob for extra data |
| `queue_order` | `int` | `0` | Priority order position within its queue |
| `queue_id` | `str` | `""` | Named queue membership (`""` resolves to default queue) |
| `fetching_metadata_since` | `str` | `""` | ISO 8601 UTC timestamp when magnet metadata fetching began |
| `uploaded_size` | `int` | `0` | Total cumulative uploaded/seeded bytes |
| `last_seeded_at` | `str` | `""` | ISO 8601 UTC timestamp of the most recent seed |
| `seeding_started_at` | `str` | `""` | ISO 8601 UTC timestamp when active seeding session began |

#### Transient Fields (not stored in DB)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `speed` | `float` | `0.0` | Current download speed (bytes/sec) |
| `eta_seconds` | `float` | `0.0` | Estimated time remaining (seconds) |
| `seeds` | `int` | `0` | Number of seeders (torrent only) |
| `peers` | `int` | `0` | Number of peers (torrent only) |
| `total_seeds` | `int` | `0` | Estimated total swarm seeds (torrent only) |
| `total_peers` | `int` | `0` | Estimated total swarm peers (torrent only) |
| `upload_speed` | `float` | `0.0` | Upload speed (torrent seeding) |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `progress` | `float` | Download percentage (0.0–100.0) |
| `metadata` | `dict` | Parsed `metadata_json` (get/set) |

---

### `QueueInfo`

```python
@dataclass
class QueueInfo
```

Represents a named download queue with local concurrency and bandwidth limits.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | `"default"` | Queue UUID (or `"default"`, `"queue-animepahe"`, `"queue-youtube"`) |
| `name` | `str` | `"Default"` | Unique display name |
| `max_concurrent` | `int` | `3` | Local concurrency ceiling (`<= 0` means unlimited within queue) |
| `position` | `int` | `0` | User display position in switcher |
| `is_default` | `bool` | `True` | `True` for the default fallback queue |
| `color` | `str` | `"#58a6ff"` | Hex swatch color (`#rrggbb`) |
| `download_limit` | `int` | `0` | Download ceiling in bytes/sec (`0` = global limit applies) |
| `upload_limit` | `int` | `0` | Upload ceiling in bytes/sec (`0` = global limit applies) |
| `created_at` | `str` | `""` | ISO 8601 UTC timestamp |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `effective_max_concurrent` | `int` | Concurrency ceiling with `<= 0` resolved to unlimited (`0`) |

---

### `SegmentEntry`

```python
@dataclass
class SegmentEntry
```

Represents one segment of an HTTP segmented download.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | `""` | UUID primary key |
| `download_id` | `str` | `""` | Foreign key to `DownloadEntry.id` |
| `index` | `int` | `0` | Segment index (0-based) |
| `start_byte` | `int` | `0` | First byte offset in file |
| `end_byte` | `int` | `0` | Last byte offset in file |
| `downloaded_bytes` | `int` | `0` | Bytes written for this segment |
| `status` | `str` | `"pending"` | One of: `pending`, `downloading`, `completed`, `error` |

---

### `Database`

```python
class Database(db_path: Path | str | None = None)
```

Synchronous SQLite wrapper for download persistence.

#### Lifecycle

| Method | Description |
|--------|-------------|
| `open()` | Connect to database, create tables if needed (WAL mode) |
| `close()` | Close the connection |

#### Download Operations

| Method | Signature | Description |
|--------|-----------|-------------|
| `add_download` | `(entry: DownloadEntry) → DownloadEntry` | Insert new download, auto-generates UUID and timestamp |
| `update_download` | `(entry: DownloadEntry) → None` | Update all fields of an existing download |
| `update_progress` | `(download_id, downloaded_size, status=None) → None` | Update byte count and optionally status |
| `update_status` | `(download_id, status, error_message="") → None` | Update status with automatic timestamp handling |
| `increment_retry` | `(download_id) → int` | Increment retry count, return new count |
| `get_download` | `(download_id) → DownloadEntry | None` | Fetch single download by ID |
| `get_all_downloads` | `(queue_id=ALL_QUEUES) → list[DownloadEntry]` | Fetch downloads, newest first (default: all queues) |
| `find_by_url` | `(url) → DownloadEntry | None` | Find download by URL (for deduplication) |
| `find_by_info_hash` | `(info_hash) → DownloadEntry | None` | Find torrent by info-hash |
| `delete_download` | `(download_id) → None` | Remove download and cascade-delete segments |
| `move_download` | `(download_id, new_save_path, new_file_path) → None` | Update save/file paths |
| `get_next_queue_order` | `(queue_id="") → int` | Get next sequence position within queue |
| `update_queue_order` | `(download_id, new_order) → None` | Update explicit queue order position |
| `swap_queue_order` | `(id1, id2) → None` | Swap order positions of two entries |

#### Named Queue Operations

| Method | Signature | Description |
|--------|-----------|-------------|
| `get_queues` | `() → list[QueueInfo]` | All queues ordered by default first then position |
| `get_queue` | `(queue_id) → QueueInfo | None` | Fetch single queue (blank resolves to default) |
| `get_default_queue` | `() → QueueInfo` | Fetch the default pinned queue |
| `get_queue_by_name` | `(name) → QueueInfo | None` | Case-insensitive lookup by display name |
| `create_queue` | `(name, max_concurrent=3, color="", download_limit=0, upload_limit=0) → tuple[bool, str]` | Create queue with concurrency & speed limits |
| `rename_queue` | `(queue_id, name) → tuple[bool, str]` | Rename non-default queue |
| `set_queue_max_concurrent` | `(queue_id, max_concurrent) → None` | Set local concurrency ceiling |
| `set_queue_limits` | `(queue_id, download_limit, upload_limit) → None` | Set download and upload bandwidth ceilings |
| `set_queue_color` | `(queue_id, color) → tuple[bool, str]` | Set queue swatch color |
| `move_queue_position` | `(queue_id, delta) → None` | Reorder queue in switcher |
| `delete_queue` | `(queue_id) → tuple[bool, str]` | Delete queue and re-home downloads to Default |
| `reassign_queue` | `(download_ids, queue_id) → int` | Reassign downloads to target queue |
| `get_active_counts_by_queue` | `() → dict[str, int]` | Active transfers count grouped by queue |
| `get_queue_download_counts` | `() → dict[str, int]` | Total downloads count grouped by queue |
| `resolve_queue_id` | `(queue_id) → str` | Maps blank or dangling queue ID to default |

#### Segment Operations

| Method | Signature | Description |
|--------|-----------|-------------|
| `add_segments` | `(segments: list[SegmentEntry]) → None` | Insert segments for a download |
| `get_segments` | `(download_id) → list[SegmentEntry]` | Get segments ordered by index |
| `update_segment` | `(segment_id, downloaded_bytes, status) → None` | Update segment progress |
| `delete_segments` | `(download_id) → None` | Remove all segments for a download |

#### UI State Operations

| Method | Signature | Description |
|--------|-----------|-------------|
| `set_ui_state` | `(key, value) → None` | Store JSON-serialized key-value state |
| `get_ui_state` | `(key, default=None) → Any` | Retrieve JSON-serialized state |
| `save_window_state` | `(state: dict) → None` | Save window geometry and table column widths |
| `get_window_state` | `() → dict` | Retrieve saved window geometry and columns |
| `save_preferences_window_size` | `(width: int, height: int) → None` | Save preferences dialog dimensions |
| `get_preferences_window_size` | `() → dict` | Retrieve saved preferences dialog dimensions |

---

## `my_idm.http_engine`

### Constants

| Name | Value | Description |
|------|-------|-------------|
| `DEFAULT_SEGMENTS` | `8` | Default parallel connections |
| `MAX_SEGMENT_STAGGER_TOTAL_S` | `2.0` | Ceiling on the total `segment_start_delay_ms` stagger before a download's last segment starts |
| `CHUNK_SIZE` | `65536` | 64 KiB read buffer |
| `MAX_RETRIES_PER_SEGMENT` | `5` | Max retries per segment |
| `RETRY_BASE_DELAY` | `1.0` | Base delay for exponential backoff (seconds) |
| `CONNECT_TIMEOUT` | `30` | TCP connect timeout (seconds) |
| `READ_TIMEOUT` | `60` | Socket read timeout (seconds) |

### Type Aliases

```python
ProgressCallback = Callable[[str, int, int, float, float], None]
# (download_id, downloaded_bytes, total_bytes, speed_bps, eta_seconds)

StatusCallback = Callable[[str, str, str], None]
# (download_id, status, error_message)
```

### `HTTPEngine`

```python
class HTTPEngine(db: Database, max_concurrent: int = 3)
```

Manages HTTP(S) downloads with segmented parallel streaming.

| Method | Signature | Description |
|--------|-----------|-------------|
| `set_callbacks` | `(progress_cb, status_cb) → None` | Register progress and status callbacks |
| `start` | `async () → None` | Initialize aiohttp session |
| `stop` | `async () → None` | Pause all downloads and close session |
| `add` | `async (entry: DownloadEntry) → None` | Start or resume a download |
| `pause` | `async (download_id) → None` | Pause download, save segment progress |
| `cancel` | `async (download_id) → None` | Cancel and clean up a download |
| `is_active` | `(download_id) → bool` | Check if download is currently running |

---

## `my_idm.torrent_engine`

### Constants

| Name | Value | Description |
|------|-------|-------------|
| `FASTRESUME_DIR` | `~/.my-idm/fastresume/` | Directory for fast-resume data |

### `TorrentEngine`

```python
class TorrentEngine(db: Database)
```

Manages torrent downloads via libtorrent. Gracefully degrades if libtorrent is not installed.

| Property | Type | Description |
|----------|------|-------------|
| `available` | `bool` | Whether libtorrent is importable |

| Method | Signature | Description |
|--------|-----------|-------------|
| `set_callbacks` | `(progress_cb, status_cb) → None` | Register callbacks |
| `start` | `() → None` | Initialize libtorrent session |
| `stop` | `() → None` | Save resume data and destroy session |
| `add_torrent` | `(entry: DownloadEntry) → bool` | Add magnet or .torrent file. Returns `False` if unavailable |
| `pause` | `(download_id) → None` | Pause torrent and save resume data |
| `resume` | `(download_id) → None` | Resume paused torrent |
| `remove` | `(download_id, delete_files=False) → None` | Remove torrent from session |
| `recheck` | `(download_id) → None` | Force piece verification |
| `move_storage` | `(download_id, new_path) → None` | Move torrent data to new directory |
| `get_status` | `(download_id) → dict | None` | Get current torrent metrics |
| `poll_all` | `() → None` | Poll all handles and emit callbacks. Call periodically. |

#### `get_status()` Return Dict

```python
{
    "total_size": int,      # Total wanted bytes
    "downloaded": int,      # Bytes downloaded
    "progress": float,      # 0-100 percentage
    "state": str,           # "downloading", "seeding", "checking_files", etc.
    "speed": float,         # Download rate (bytes/sec)
    "upload_speed": float,  # Upload rate (bytes/sec)
    "seeds": int,           # Number of seeders
    "peers": int,           # Number of peers
    "eta": float,           # Estimated seconds remaining
    "name": str,            # Torrent name (if metadata resolved)
}
```

---

## `my_idm.manager`

### Constants

| Name | Value | Description |
|------|-------|-------------|
| `DEFAULT_SAVE_PATH` | `~/Downloads` | Default download directory |

### `DownloadManager`

```python
class DownloadManager(QObject)
```

Central orchestrator connecting GUI, engines, and database.

#### Qt Signals

| Signal | Parameters | Description |
|--------|------------|-------------|
| `progress_updated` | `(str, int, int, float, float, int, int, float)` | `download_id, downloaded, total, speed, eta, seeds, peers, upload_speed` |
| `status_changed` | `(str, str, str)` | `download_id, status, error_message` |
| `filename_resolved` | `(str, str)` | `download_id, filename` (emitted when server headers or metadata resolve filename) |
| `download_added` | `(str)` | `download_id` |
| `download_removed` | `(str)` | `download_id` |
| `download_moved` | `(str)` | `download_id` |
| `download_renamed` | `(str, str)` | `download_id, new_filename` (emitted when root file or folder is renamed) |
| `bandwidth_limits_changed` | `(int, int)` | `download_limit, upload_limit` (bytes/s, 0 = unlimited) |
| `general_config_changed` | `(object)` | Emitted when `GeneralConfig` preferences change |
| `torrent_config_changed` | `(object)` | Emitted when `TorrentConfig` preferences change |
| `network_config_changed` | `(object)` | Emitted when `NetworkConfig` preferences change |
| `security_config_changed` | `(object)` | Emitted when `SecurityConfig` preferences change |
| `tor_config_changed` | `(object)` | Emitted when `TorConfig` preferences change |
| `threat_detected` | `(str, str)` | `download_id, report` (emitted on malware detection) |
| `queue_order_changed` | `()` | Emitted when priorities or queue order change |
| `queues_changed` | `()` | Emitted when queue definitions, limits, or order change |
| `queue_scope_changed` | `(str)` | Emitted when active queue view scope changes (`""` = all) |
| `tor_status_changed` | `(str, str)` | `status, message` (Tor connection lifecycle) |
| `tor_availability_changed` | `(bool)` | Emitted when a Tor SOCKS5 endpoint is reachable/lost |
| `external_tools_config_changed` | `(object)` | Emitted when external tools preferences change |
| `animepahe_status_changed` | `(bool)` | Emitted when AnimePahe daemon starts/stops |
| `animepahe_queue_changed` | `(int)` | Emitted with AnimePahe pending queue size |
| `browser_config_changed` | `(object)` | Emitted when browser integration settings change |
| `process_backlogs_requested` | `()` | Request to process backlog files on main thread |
| `youtube_error` | `(str, str)` | `source_url, error_message` |

#### Lifecycle

| Method | Description |
|--------|-------------|
| `start()` | Start asyncio thread, HTTP engine, torrent engine, and timers |
| `stop()` | Stop everything cleanly (save state, close connections) |

#### Download Management

| Method | Signature | Description |
|--------|-----------|-------------|
| `add_download` | `(url, save_path="", num_segments=8, queue="") → str | None` | Add download with dedup and queue assignment. Returns ID or None |
| `pause_download` | `(download_id) → None` | Pause active download |
| `resume_download` | `(download_id) → None` | Resume paused/errored download |
| `delete_download` | `(download_id, delete_files=False) → None` | Remove download and optionally delete files |
| `move_download` | `(download_id, new_save_path) → None` | Move download to new directory |
| `rename_download` | `(download_id, new_filename) → tuple[bool, str]` | Rename root file or folder on disk and DB at any point in time |
| `recheck_download` | `(download_id) → None` | Verify file integrity |
| `set_bandwidth_limits` | `(download_limit, upload_limit) → None` | Set global down/up bandwidth limits in bytes/s |
| `set_download_bandwidth_allocation` | `(download_id, allocation) → None` | Set download allocation ('low', 'medium', 'high', 'max') |
| `get_download_bandwidth_allocation` | `(download_id) → str` | Get download allocation |
| `load_backlog` | `(filepath) → int` | Load URLs with optional download locations from file, auto-clearing processed entries |
| `process_backlogs` | `(extra_filepath=None) → int` | Scan project root, app dir, user home & custom places for backlog files |

#### Queue Management

| Method | Signature | Description |
|--------|-----------|-------------|
| `get_queues` | `() → list[QueueInfo]` | Retrieve all queues in display order |
| `get_queue` | `(queue_id) → QueueInfo | None` | Fetch queue by ID |
| `create_queue` | `(name, max_concurrent=3, color="", download_limit=0, upload_limit=0) → tuple[bool, str]` | Create a named queue |
| `rename_queue` | `(queue_id, name) → tuple[bool, str]` | Rename queue |
| `set_queue_max_concurrent` | `(queue_id, max_concurrent) → None` | Update queue local concurrency budget |
| `set_queue_limits` | `(queue_id, download_limit, upload_limit) → None` | Update queue download and upload ceilings |
| `set_queue_color` | `(queue_id, color) → tuple[bool, str]` | Update queue swatch color |
| `move_queue` | `(queue_id, delta) → None` | Move queue up/down in switcher |
| `delete_queue` | `(queue_id) → tuple[bool, str]` | Delete queue and re-home its downloads to Default |
| `reassign_queue` | `(download_ids, queue_id) → int` | Move downloads into another queue |

#### Query

| Method | Signature | Description |
|--------|-----------|-------------|
| `get_all_entries` | `() → list[DownloadEntry]` | All downloads from DB |
| `get_entry` | `(download_id) → DownloadEntry | None` | Single download by ID |

---

## `my_idm.download_model`

### `Col`

Column index constants and header labels.

| Constant | Index | Header |
|----------|-------|--------|
| `QUEUE` | 0 | "#" |
| `NAME` | 1 | "Name" |
| `SOURCE_DOMAIN` | 2 | "Source Domain" |
| `SIZE` | 3 | "Size" |
| `PROGRESS` | 4 | "Progress" |
| `STATUS` | 5 | "Status" |
| `SPEED` | 6 | "Speed" |
| `ETA` | 7 | "ETA" |
| `SEEDS_PEERS` | 8 | "Seeds / Peers" |
| `ADDED` | 9 | "Added" |
| `LAST_TRIED` | 10 | "Last Tried" |
| `COMPLETED` | 11 | "Completed" |
| `SAVE_PATH` | 12 | "Save Path" |
| `FILE_NAME` | 13 | "File / Folder Name" |
| `LAST_SEEDED` | 14 | "Last Seeded" |
| `SOURCE` | 15 | "Source" |
| `SEEDING_STARTED_AT` | 16 | "Seeding Started At" |
| `QUEUE_NAME` | 17 | "Queue" |
| `COUNT` | 18 | Total column count |

### `DownloadTableModel`

```python
class DownloadTableModel(QAbstractTableModel)
```

Table model backed by a list of `DownloadEntry` objects. The `SOURCE_DOMAIN` column displays the hostname extracted from each URL or magnet tracker/webseed, while `NAME` contains only the filename or fallback URL.

| Method / Property | Signature | Description |
|---|---|---|
| `sort_column` | `int` (property) | Currently active sort column (defaults to `Col.ADDED`) |
| `sort_order` | `Qt.SortOrder` (property) | Currently active sort order (defaults to `DescendingOrder`) |
| `sort` | `(column: int, order: Qt.SortOrder) → None` | Sort entries and remap persistent selection indexes |
| `load_entries` | `(entries: list[DownloadEntry]) → None` | Full model reset with automatic sort application |
| `add_entry` | `(entry: DownloadEntry) → None` | Inserts row in correct sorted position |
| `remove_entry` | `(download_id: str) → None` | Remove row by ID |
| `get_entry` | `(row: int) → DownloadEntry | None` | Get entry by row index (returns None for section headers) |
| `get_entry_by_id` | `(download_id: str) → DownloadEntry | None` | Get entry by ID |
| `get_selected_ids` | `(indexes: list[QModelIndex]) → list[str]` | Extract unique non-header download IDs from selection |
| `update_filename` | `(download_id: str, filename: str) → None` | Update entry filename and file_path dynamically |
| `update_progress` | `(download_id, downloaded, total, speed, eta, seeds, peers, upload_speed) → None` | Efficient partial update |
| `update_status` | `(download_id, status, error_msg) → None` | Status and color update |
| `refresh_entry` | `(download_id, entry) → None` | Full row refresh |
| `set_segregated_view` | `(enabled: bool, mode: str | None = None) → None` | Toggle segregated view and optionally switch grouping mode |
| `is_segregated_view` | `() → bool` | Returns True if segregated view is enabled |
| `set_segregated_mode` | `(mode: str) → None` | Set active segregation grouping mode (`"status"` or `"date"`) |
| `segregated_mode` | `() → str` | Returns currently active grouping mode (`"status"` or `"date"`) |
| `set_section_collapsed` | `(section_id: str, collapsed: bool) → None` | Set collapsed state for a section ID |
| `is_section_collapsed` | `(section_id: str) → bool` | Returns True if specified section ID is collapsed |
| `is_section_header_row` | `(row: int) → bool` | Returns True if the specified row is a section header |
| `get_section_header_row_indices` | `() → list[int]` | Returns visual row indices of all active section headers |
| `toggle_section_collapsed` | `(row: int) → tuple[str, bool] | None` | Toggles collapsed state for header at row index |

### Section Constants

| Constant | Value | Description |
|---|---|---|
| `SECTION_ACTIVE` | `"active"` | Status mode: Active transfers header |
| `SECTION_SEEDING` | `"seeding"` | Status mode: Seeding transfers header |
| `SECTION_INACTIVE` | `"inactive"` | Status mode: Inactive/completed transfers header |
| `SECTION_DATE_TODAY` | `"date_today"` | Date mode: Today header |
| `SECTION_DATE_YESTERDAY` | `"date_yesterday"` | Date mode: Yesterday header |
| `SECTION_DATE_LAST_7_DAYS` | `"date_last_7_days"` | Date mode: Last 7 Days header (alias: `SECTION_DATE_THIS_WEEK`) |
| `SECTION_DATE_LAST_30_DAYS` | `"date_last_30_days"` | Date mode: Last 30 Days header (alias: `SECTION_DATE_THIS_MONTH`) |
| `SECTION_DATE_OLDER` | `"date_older"` | Date mode: Older / dateless header |

### Helper Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_entry_latest_timestamp` | `(entry: DownloadEntry) → datetime | None` | Returns the latest datetime among added_at, completed_at, and last_tried_at |
| `get_entry_date_category` | `(entry: DownloadEntry, now_dt: datetime | None = None) → str` | Classifies entry into Today, Yesterday, Last 7 Days, Last 30 Days, or Older |
| `_format_speed` | `(bps: float) → str` | e.g. `"1.5 MiB/s"` or `"—"` |
| `_format_eta` | `(seconds: float) → str` | e.g. `"5m 30s"` or `"2h 15m"` |
| `_format_time` | `(iso_str: str) → str` | ISO 8601 → `"YYYY-MM-DD HH:MM"` (local time) |

---

## `my_idm.delegates`

### `ProgressBarDelegate`

```python
class ProgressBarDelegate(QStyledItemDelegate)
```

Renders a styled, color-coded progress bar in a `QTableView` cell.

**Expected model data** (via `Qt.DisplayRole`):

```python
{"progress": float, "status": str}
```

**Status → Color mapping:**

| Status | Color | Hex |
|--------|-------|-----|
| `downloading` | Blue | `#1f6feb` |
| `completed` | Green | `#238636` |
| `seeding` | Purple | `#8957e5` |
| `paused` | Amber | `#9e6a03` |
| `error` | Red | `#da3633` |
| `queued` | Gray | `#6e7681` |

### `DownloadNameDelegate`

```python
class DownloadNameDelegate(QStyledItemDelegate)
```

Renders the filename in the `NAME` column while preserving decoration icons and removing the duplicate text form of the active Tor indicator. Source domains are supplied by the model's separate `SOURCE_DOMAIN` column rather than this delegate.

---
## `my_idm.dialogs`

### `AddDownloadDialog`

```python
class AddDownloadDialog(QDialog)
```

Dialog for adding a new download.

| Property / Method | Type | Description |
|-------------------|------|-------------|
| `url` | `str` | Entered URL, magnet link, or .torrent path |
| `save_path` | `str` | Selected save directory |
| `num_segments` | `int` | Selected segment count (1–32) |
| `is_tor_enabled()` | `bool` | Current state of Tor privacy routing button |

### `MoveDownloadDialog`

```python
class MoveDownloadDialog(QDialog)
```

Dialog for selecting a new save directory.

| Property | Type | Description |
|----------|------|-------------|
| `new_path` | `str` | Selected new directory path |

### `DeleteConfirmDialog`

```python
class DeleteConfirmDialog(QDialog)
```

Confirmation dialog for deleting downloads.

| Property | Type | Description |
|----------|------|-------------|
| `delete_files` | `bool` | Whether the user checked "Also delete files" |

---

## `my_idm.styles`

### `Colors`

Class containing all color constants used throughout the application.

| Constant | Value | Usage |
|----------|-------|-------|
| `BG_DARK` | `#0d1117` | Main background |
| `BG_MID` | `#161b22` | Toolbar, menus, headers |
| `BG_LIGHT` | `#21262d` | Buttons, hover states |
| `BG_HOVER` | `#30363d` | Hover highlight |
| `BG_SELECTED` | `rgba(46, 160, 67, 0.22)` | Selected row (soft translucent green) |
| `BORDER` | `#30363d` | General borders |
| `BORDER_LIGHT` | `#484f58` | Lighter borders |
| `TEXT` | `#e6edf3` | Primary text |
| `TEXT_SECONDARY` | `#8b949e` | Secondary text, headers |
| `TEXT_DIM` | `#6e7681` | Dimmed text |
| `ACCENT` | `#58a6ff` | Links, active elements |
| `GREEN` | `#3fb950` | Success, completed |
| `ORANGE` | `#d29922` | Warning, paused |
| `RED` | `#f85149` | Error, danger |
| `PURPLE` | `#bc8cff` | Seeding, special |

### `DARK_STYLESHEET`

A complete QSS stylesheet string applied to `QApplication`. Covers all widget types: `QMainWindow`, `QMenuBar`, `QMenu`, `QToolBar`, `QToolButton`, `QTableView`, `QHeaderView`, `QScrollBar`, `QStatusBar`, `QDialog`, `QLabel`, `QLineEdit`, `QSpinBox`, `QPushButton`, `QCheckBox`, `QToolTip`, `QGroupBox`.

---

## `my_idm.single_instance`

### `SingleInstanceManager`

```python
class SingleInstanceManager(QObject)
```

Enforces a single running instance of My-IDM using `QLocalServer` and `QLocalSocket` IPC.

| Method / Signal | Signature | Description |
|-----------------|-----------|-------------|
| `send_message(payload, timeout_ms=1500)` | `(dict, int) -> bool` | Transmits JSON payload to the active primary instance. Returns `True` if connected and delivered. |
| `start_server()` | `() -> bool` | Starts the `QLocalServer` for the primary instance, clearing stale pipes. |
| `close()` | `() -> None` | Shuts down server and removes pipe registration. |
| `message_received` | `Signal(dict)` | Emitted when a secondary instance connects with payload data. |

### `activate_window(window)`

```python
def activate_window(window: Optional[QWidget]) -> None
```

Restores minimized windows (`showNormal()`), raises to top (`raise_()`), and requests foreground input focus across platforms (including Windows `SetForegroundWindow` integration).
