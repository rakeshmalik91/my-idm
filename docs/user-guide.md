# User Guide

Welcome to **My-IDM** — a full-featured download manager with a dark-themed GUI.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Adding Downloads](#adding-downloads)
- [Managing Downloads](#managing-downloads)
- [Backlog Files](#backlog-files)
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [Command-Line Options](#command-line-options)
- [Data & File Locations](#data--file-locations)
- [Troubleshooting](#troubleshooting)

---

## Getting Started

### Prerequisites

- Python 3.11 or later
- `pip` package manager

### Installation

```bash
# Install dependencies from requirements.txt
pip install -r requirements.txt
```

> **Note:** `libtorrent` is optional. If it cannot be installed on your system, the app will still work for HTTP downloads — torrent support will be disabled.

### Launch

```bash
python -m my_idm.main
```

The main window opens with an empty download list. The status bar at the bottom shows "Ready" and "0 download(s)".

---

## Adding Downloads

### Add a Download (URL, Magnet Link, or .torrent File)

1. Click **➕ Add Download** in the toolbar (or press **Ctrl+N**)
2. The dialog **automatically detects and prefills** any valid URL, magnet link, or `.torrent` file path found in your clipboard (with the text pre-selected for quick replacement).
3. For `.torrent` files, you can paste the file path directly or click the built-in **Browse .torrent …** button (or use File → **Add Torrent File…** / **Ctrl+T**).
4. Choose a save directory (defaults to `~/Downloads`)
5. Set the number of segments for parallel downloading (1–32, default 8 for HTTP)
6. Click **Download** (or press Enter)

### Supported Input Types

| Input | Example | Type |
|-------|---------|------|
| HTTP/HTTPS URL | `https://example.com/file.zip` | HTTP segmented download |
| Magnet link | `magnet:?xt=urn:btih:HASH&dn=name` | Torrent via libtorrent |
| `.torrent` file path | `D:\Torrents\file.torrent` | Torrent via libtorrent |

### Duplicate Detection

My-IDM automatically detects duplicates:

- **HTTP downloads** are matched by URL
- **Torrents** are matched by info-hash

If you add a URL that already exists:
- If **completed** → the add is silently ignored
- If **paused / errored / queued** → the existing download is automatically resumed
- If **already downloading** → no duplicate is created

---

## Managing Downloads

### Pause & Resume

- Select one or more downloads in the list
- Click **⏸** (Pause icon on toolbar) or press **Space** to pause
- Click **▶** (Play/Resume icon on toolbar) or press **Ctrl+R** to resume
- The Edit menu and right-click context menu also provide full text **Resume** and **Pause** actions.

Paused downloads save their progress to disk:
- **HTTP**: Per-segment byte offsets are saved to the database. On resume, only the remaining bytes are fetched.
- **Torrents**: Fast-resume data is saved to `~/.my-idm/fastresume/`. On resume, libtorrent picks up from where it left off.

### Delete

1. Select one or more downloads
2. Click **🗑 Delete** or press **Delete**
3. A confirmation dialog appears with the option **"Also delete downloaded files from disk"**
4. Click **Delete** to confirm

### Move

1. Select one or more downloads
2. Click **📂 Move** in the toolbar
3. Choose a new save directory
4. Click **Move**

Moving works without breaking active downloads:
- **HTTP**: The download is paused → file is physically moved → database is updated → download resumes
- **Torrents**: `libtorrent` handles `move_storage()` natively — no interruption needed

### Recheck

1. Select one or more downloads
2. Click **🔄 Recheck** in the toolbar

Rechecking verifies existing files:
- **Torrents**: Runs `force_recheck()` — libtorrent verifies every piece against its hash
- **HTTP**: Compares file size on disk vs. the expected total size. If the file is fully downloaded, it's marked as completed. If the file is missing, progress is reset.

### Open File / Open Folder

- **📄 Open File** (Enter) — Opens the downloaded file with the default application
- **📁 Open Folder** (Ctrl+O) — Opens the containing folder in Explorer with the file selected

### Context Menu

Right-click any download to access all actions (Pause, Resume, Recheck, Move, Open File, Open Folder, Delete).

---

## The Download List

The main table shows 12 columns of information for each download:

| Column | Description |
|--------|-------------|
| **Name** | Filename (auto-updates when resolved from headers or metadata; hover for full path) |
| **Size** | Total file size (human-readable, e.g. "1.5 GiB") |
| **Progress** | Color-coded progress bar with percentage |
| **Status** | Current state (see below) |
| **Speed** | Download speed (or upload speed when seeding) |
| **ETA** | Estimated time remaining |
| **Type** | `HTTP` or `TORRENT` |
| **Seeds / Peers** | For torrents: `S:5 P:12`. For HTTP: `8 seg` |
| **Added** | When the download was first added |
| **Last Tried** | When the last download attempt started |
| **Completed** | When the download finished |
| **Save Path** | Directory where the file is saved |

### Status Values

| Status | Color | Meaning |
|--------|-------|---------|
| Queued | Gray | Waiting to start or queued for retry |
| Downloading | Blue | Actively downloading |
| Paused | Orange | Paused by the user |
| Completed | Green | Successfully finished |
| Seeding | Purple | Torrent is seeding (uploading to others) |
| Error ⚠ | Red | Failed (hover for error details) |
| Checking | Orange | Rechecking file integrity |

### Progress Bar Colors

The progress bar changes color based on status:
- 🔵 **Blue** — Downloading
- 🟢 **Green** — Completed
- 🟠 **Orange** — Paused
- 🔴 **Red** — Error
- 🟣 **Purple** — Seeding

### Sorting the List

The download list supports full column sorting:

- **Default Sort**: By **Added** date, **Descending (DESC)** — newest downloads always appear at the top.
- **Click Column Headers**: Click any column header to sort by that column. Click again to toggle between Ascending (▲) and Descending (▼) order.
- **View Menu**: Use **View → Sort By** to select a sort column (Date Added, Name, Size, Progress, Status, Speed, ETA, Date Completed) and choose Ascending or Descending order.
- **Selection Preserved**: Sorting keeps your selected rows highlighted even when their row positions change.
- **Smart Sorting**: 
  - Downloads with active ETAs appear before inactive ones.
  - Completed downloads sort above uncompleted downloads when sorting by Completed date.
  - New downloads automatically insert into their proper sorted position without resetting the view.

### Resizing Columns

All table columns are interactively resizable:
- Hover your mouse over the vertical divider line between any two column headers.
- Click and drag to adjust column widths to your preference.
- Your customized column widths are automatically saved and restored on subsequent application launches.

---

## Backlog Files

A backlog file is a text file with one URL per line. My-IDM can load backlog files in two ways:

### Automatic Loading on Startup

If `~/.my-idm/backlog.txt` exists, it is automatically loaded when the app starts.

### Manual Loading

1. Click **File → Load Backlog** (or press **Ctrl+L**)
2. Select a `.txt` file
3. The status bar shows how many downloads were added

### Backlog File Format

```
# Lines starting with # are comments
# Empty lines are ignored

# HTTP downloads
https://example.com/file1.zip
https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso

# Magnet links
magnet:?xt=urn:btih:EXAMPLE_HASH&dn=example_file

# .torrent file paths
D:\Torrents\example.torrent
```

Duplicate URLs are automatically skipped. See [backlog.txt.example](../backlog.txt.example) for a reference.

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| **Ctrl+N** | Add Download (URL, Magnet link, or .torrent) |
| **Ctrl+T** | Add .torrent file directly |
| **Ctrl+R** | Resume selected downloads (Play) |
| **Space** | Pause selected downloads |
| **Delete** | Delete selected downloads |
| **Enter** | Open downloaded file |
| **Ctrl+O** | Open containing folder |
| **Ctrl+L** | Load backlog file |
| **Ctrl+A** | Select all downloads |
| **Ctrl+Q** | Quit the application |

---

## Command-Line Options

```
usage: python -m my_idm.main [-h] [--backlog BACKLOG] [--verbose]

My-IDM — A full-featured download manager

options:
  -h, --help            show this help message and exit
  --backlog BACKLOG, -b BACKLOG
                        Path to a backlog file with URLs (one per line)
  --verbose, -v         Enable verbose (debug) logging
```

### Examples

```bash
# Launch normally
python -m my_idm.main

# Launch with a specific backlog file
python -m my_idm.main --backlog urls.txt

# Launch with debug logging
python -m my_idm.main -v

# Both
python -m my_idm.main -b urls.txt -v
```

---

## Data & File Locations

| Item | Path |
|------|------|
| Application data | `~/.my-idm/` |
| Download database | `~/.my-idm/downloads.db` |
| Log file | `~/.my-idm/my-idm.log` |
| Torrent fast-resume | `~/.my-idm/fastresume/` |
| Default backlog | `~/.my-idm/backlog.txt` |
| Default save directory | `~/Downloads/` |

> On Windows, `~` expands to `C:\Users\<username>`.

---

## How Downloads Work

### HTTP Downloads

1. A `HEAD` request is sent to discover the file size and check for `Accept-Ranges: bytes` support
2. **If range requests are supported**: the file is split into segments (default 8) and downloaded in parallel
3. **If range requests are NOT supported**: the file is downloaded in a single stream
4. If a segmented download fails (e.g. server returns 416 or 403 on a range request), the engine automatically falls back to a single-stream download
5. Each segment retries up to 5 times with exponential backoff (1s, 2s, 4s, 8s, 16s)
6. The file is pre-allocated to its full size on disk so segments can write to their respective offsets

### Torrent Downloads

1. The magnet link or .torrent file is added to the libtorrent session
2. For magnets, metadata is first resolved from the DHT/peers
3. The download proceeds using the BitTorrent protocol
4. After completion, the torrent remains in a seeding state until manually paused
5. Fast-resume data is saved on pause and shutdown for instant restart

### Automatic Retries

- HTTP segments retry up to 5 times with exponential backoff
- A global retry timer checks every 10 seconds for any queued downloads that need re-attempting
- Downloads that exhaust all retries are marked as "Error"

### Resume on Restart

When the app starts, any downloads that were in "downloading" or "queued" state are automatically resumed.

---

## Troubleshooting

### "libtorrent not installed — torrent support disabled"

This means the `libtorrent` Python package could not be imported. Install it with:

```bash
pip install libtorrent
```

If pip cannot find a compatible wheel for your Python version, try installing Python 3.12 and using its pip:

```bash
py -3.12 -m pip install libtorrent
```

The app will still work for HTTP downloads without libtorrent.

### Download stuck at 0%

- Check that the URL is accessible (try opening it in a browser)
- Check `~/.my-idm/my-idm.log` for error details
- Some servers block range requests — the download should automatically fall back to single-stream

### Progress bar shows wrong percentage

Select the download and click **🔄 Recheck** to re-verify the file against its expected size.

### App won't start

Ensure all dependencies are installed:

```bash
pip install PySide6 aiohttp aiosqlite humanize
```

Run with verbose mode to see detailed logs:

```bash
python -m my_idm.main -v
```
