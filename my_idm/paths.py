"""Platform-correct application directories, with a legacy-location fallback.

Four modules used to hardcode `Path.home() / ".my-idm"`, and a dotfile in `$HOME` is a Windows
convention: on Linux and macOS the standard locations are `$XDG_DATA_HOME` and
`~/Library/Application Support`. Anything that looks for My-IDM's data - a backup script, a support
request, a second tool reading the download history - has to know where to look, and "the same
place, but by a different rule per platform" is not a rule.

Resolution, in order:

1. An explicit override, so tests and `--data-dir` style flags never touch the real profile.
2. The platform's standard directory.
3. `~/.my-idm`, **if it already exists** - see below.

Why the legacy path still wins when present
-------------------------------------------
A user's `downloads.db` is their download history: every URL, every completed file, hours of
seeding. Migrating it unattended means opening a live SQLite database the app may be holding, and
getting it wrong splits one user's history across two directories in a way they cannot see and
cannot undo. So this module does not migrate anything.

It also does not relocate the directory out from under a running install. A user who already has
`~/.my-idm` keeps using it - on every platform - so the app behaves identically to the previous
version and no data moves. A fresh install on Linux or macOS gets the standard location instead,
which is what makes the difference observable to anyone starting clean.

The trade-off is stated plainly: a user with a pre-existing `~/.my-idm` on Linux keeps the
non-standard location until they move it themselves. That is the deliberate cost of not risking
their history, and it is why the legacy branch is gated on the directory *existing* rather than on
the platform.

Cached, because this is read at import time by four modules and must not change underneath them.
Call :func:`reset_cache` after changing an override.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: Directory name used inside the platform-specific parent. Lower case on purpose: it becomes
#: `~/.config/my-idm`, and XDG is specified in terms of lower-case names.
APP_DIRNAME = "my-idm"

#: Pre-Phase-0 location. Honoured when it exists so existing installs keep working.
LEGACY_DIRNAME = ".my-idm"

_override: Optional[Path] = None
_cache: dict = {}


def set_data_dir(path: Optional[Path | str]) -> None:
    """Force the data directory, bypassing platform resolution.

    For tests and for anything that needs an isolated profile. Pass ``None`` to return to normal
    resolution. Invalidates the cache.
    """
    global _override
    _override = Path(path).expanduser() if path is not None else None
    _cache.clear()


def reset_cache() -> None:
    """Drop memoised paths. Call after changing the environment (``XDG_*``, ``HOME``)."""
    _cache.clear()


def _legacy_dir() -> Path:
    return Path.home() / LEGACY_DIRNAME


def _legacy_in_use() -> bool:
    """Whether a pre-existing ``~/.my-idm`` should keep being used.

    ``is_dir()`` rather than ``exists()``: a *file* at that path is not a My-IDM profile, and
    treating it as one would then fail every write with a permission error instead of quietly
    using the correct location.
    """
    try:
        return _legacy_dir().is_dir()
    except OSError:
        # Unreadable home (no permission, exotic mount). Not a reason to fail startup.
        return False


def _xdg_dir(env_var: str, default: str) -> Path:
    """Resolve one XDG base directory, defaulting per the XDG Base Directory spec.

    An empty *or relative* value is ignored rather than honoured: the spec says such a path is
    invalid, and joining one onto a home directory would scatter application data somewhere no
    desktop environment looks.
    """
    raw = os.environ.get(env_var, "")
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path.home() / default


def _windows_base() -> Path:
    """``%APPDATA%\\My-IDM``, falling back to ``~/.my-idm``'s parent when unset.

    ``APPDATA`` is normally always set on Windows, but a service or scheduled task can launch with a
    stripped environment, and ``Path.home()`` there resolves from ``USERPROFILE``. Falling back to
    the home directory keeps such a launch working rather than writing to a relative path.
    """
    appdata = os.environ.get("APPDATA", "")
    if appdata and Path(appdata).is_absolute():
        return Path(appdata) / "My-IDM"
    return Path.home() / "AppData" / "Roaming" / "My-IDM"


def _platform_base() -> Path:
    """The directory the platform expects application data in."""
    if sys.platform == "win32":
        return _windows_base()
    if sys.platform == "darwin":
        # Both config and data live here on macOS; splitting them buys nothing for this app.
        return Path.home() / "Library" / "Application Support" / "My-IDM"
    # Linux, and BSD/other POSIX which have no more specific standard.
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / APP_DIRNAME


def data_dir() -> Path:
    """The directory holding the database, fast-resume state, Tor data and logs."""
    if "data" in _cache:
        return _cache["data"]
    if _override is not None:
        resolved = _override
    elif _legacy_in_use():
        resolved = _legacy_dir()
    else:
        resolved = _platform_base()

    if _override is None and _legacy_in_use() and resolved != _legacy_dir():
        log.info(
            "Using the legacy My-IDM data directory %s. This is fine and nothing will move; "
            "delete it only after moving your downloads.db somewhere else, or the history is lost.",
            resolved,
        )
    _cache["data"] = resolved
    return resolved


def config_dir() -> Path:
    """Directory for editable configuration.

    Currently nothing writes here - :class:`~my_idm.config.GeneralConfig` uses ``QSettings``, which
    already resolves per platform on its own (registry / ``~/.config`` / ``~/Library/Preferences``).
    Provided so that when something does need a file, it has an obvious home that is not the
    database directory, where a stray file would be swept into backups of the user's history.
    """
    if "config" in _cache:
        return _cache["config"]
    if _override is not None:
        resolved = _override
    elif sys.platform == "win32":
        resolved = _windows_base()
    elif sys.platform == "darwin":
        resolved = Path.home() / "Library" / "Application Support" / "My-IDM"
    else:
        resolved = _xdg_dir("XDG_CONFIG_HOME", ".config") / APP_DIRNAME
    _cache["config"] = resolved
    return resolved


def ensure_data_dir() -> Path:
    """Create the data directory if needed and return it.

    Called from the few places that write, rather than at import time: creating a directory as a
    side effect of importing a module makes the module unusable in a read-only or sandboxed
    context, which is exactly where a test or a dry run happens.
    """
    target = data_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"My-IDM could not create its data directory {target}: {exc}. "
            "Set a writable location with the MYIDM_DATA_DIR environment variable."
        ) from exc
    return target


def database_path() -> Path:
    """The SQLite file. Separated from :func:`data_dir` so its callers read as intent."""
    if "db" in _cache:
        return _cache["db"]
    _cache["db"] = data_dir() / "downloads.db"
    return _cache["db"]


def fastresume_dir() -> Path:
    """Per-torrent ``fastresume`` state written by libtorrent."""
    if "fastresume" in _cache:
        return _cache["fastresume"]
    _cache["fastresume"] = data_dir() / "fastresume"
    return _cache["fastresume"]


def tor_data_dir() -> Path:
    """Tor's ``--DataDirectory``: the pid file and its own state."""
    if "tor" in _cache:
        return _cache["tor"]
    _cache["tor"] = data_dir() / "tor_data"
    return _cache["tor"]


def logs_dir() -> Path:
    """Application log output."""
    if "logs" in _cache:
        return _cache["logs"]
    _cache["logs"] = data_dir() / "logs"
    return _cache["logs"]


def backlog_path() -> Path:
    """The de-duplication backlog file, written next to the log."""
    if "backlog" in _cache:
        return _cache["backlog"]
    _cache["backlog"] = data_dir() / "backlog.txt"
    return _cache["backlog"]


def default_downloads_dir() -> Path:
    """Where downloads land when the user has not chosen a location.

    Resolved via ``QStandardPaths`` rather than ``Path.home() / "Downloads"``, which is wrong on
    any Linux install that localises directory names - ``~/Загрузки``, ``~/Descargas``,
    ``~/Downloads`` on a non-English system - and would silently create a second, empty-looking
    folder next to the user's real one.
    """
    if "downloads" in _cache:
        return _cache["downloads"]
    resolved = None
    try:
        from PySide6.QtCore import QStandardPaths

        raw = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        if raw:
            resolved = Path(raw)
    except Exception:
        # No QApplication yet, or Qt refused. Fall through to the conventional path.
        resolved = None
    if resolved is None:
        resolved = Path.home() / "Downloads"
    _cache["downloads"] = resolved
    return resolved
