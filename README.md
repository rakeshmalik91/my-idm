# My-IDM — Download Manager

A full-featured download manager with GUI built in Python.

## Features

- **Segmented HTTP Downloads** — Multi-connection parallel downloading with automatic fallback to single-stream
- **Torrent Support** — `.torrent` files and magnet link support via libtorrent
- **Pause / Resume** — Pause and resume any download; state persists across app restarts
- **Automatic Retries** — Exponential backoff retry on failure (configurable max retries)
- **Download History** — SQLite database tracks all downloads with full history
- **Deduplication** — Duplicate URLs are detected; incomplete downloads are auto-resumed
- **Move Files** — Relocate downloads without breaking the download process
- **Recheck Files** — Verify existing files on disk against expected state
- **Backlog File** — Load URLs from a text file on startup (`~/.my-idm/backlog.txt`)
- **Detailed List View** — Name, size, progress bar, status, speed, ETA, type, seeds/peers, timestamps
- **Dark Theme** — GitHub-dark inspired modern UI

## Tech Stack

| Layer | Technology |
|-------|-----------|
| GUI | PySide6 (Qt6) |
| HTTP Downloads | aiohttp + asyncio |
| Torrents | libtorrent |
| Database | SQLite |

## Installation

```bash
pip install -r requirements.txt
```

Or install the package in editable mode:

```bash
pip install -e .
```

## Usage

**On Windows:**
Double-click `run.bat` or run from terminal:
```cmd
run.bat
```

**Using Python:**
```bash
python -m my_idm.main
```

### CLI Options

```
--backlog, -b <file>   Load URLs from a backlog file
--verbose, -v          Enable debug logging
```

### Backlog File Format

Create `~/.my-idm/backlog.txt` (or any text file) with one URL per line:

```
# Lines starting with # are comments
https://example.com/file.zip
magnet:?xt=urn:btih:HASH&dn=name
D:\path\to\file.torrent
```

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+N | Add URL |
| Ctrl+T | Add Torrent |
| Ctrl+R | Resume |
| Space | Pause |
| Delete | Delete |
| Ctrl+O | Open Folder |
| Enter | Open File |
| Ctrl+L | Load Backlog |
| Ctrl+Q | Quit |

## Data Storage

- **Database**: `~/.my-idm/downloads.db`
- **Logs**: `~/.my-idm/my-idm.log`
- **Torrent Resume Data**: `~/.my-idm/fastresume/`
- **Default Save Path**: `~/Downloads/`

## Documentation

- [**User Guide**](docs/user-guide.md) — Complete guide to using My-IDM: adding downloads, managing them, backlog files, keyboard shortcuts, troubleshooting
- [**Architecture Guide**](docs/architecture.md) — Developer documentation: module map, threading model, data flow, database schema, engine internals, extending the app
- [**API Reference**](docs/api-reference.md) — Full reference for every class, method, signal, constant, and type

## License

GPL-3.0-or-later