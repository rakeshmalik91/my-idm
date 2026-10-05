"""How ``.torrent`` files get into My-IDM from outside the app.

A ``.torrent`` is the one download kind the user holds as a *file* rather than a URL, so it
arrives through three channels that have nothing to do with each other and everything to do with
the same question: what local paths, if any, should be added as torrents? This module owns the
two passive channels:

* **Drag and drop** anywhere in the main window. :func:`local_torrent_paths_from_mime` turns a
  drag payload into paths, which is the whole of the logic and is kept pure so it can be tested
  without synthesising drag events.
* **A watched folder.** :class:`TorrentFolderWatcher` notices ``.torrent`` files appearing and
  reports them.

Both funnel into ``DownloadManager.add_torrent_files``, which holds the ingestion policy, so the
drop path, the watched folder and the Add Torrent button cannot disagree about what counts as a
new torrent.

Three decisions in here are the difference between the feature working and being switched off:

**Paths are canonicalised before anything else sees them.** ``DownloadManager`` de-duplicates on
an exact string match of the ``url`` column, which is neither indexed nor case-folded. A drag
yields ``D:\\Torrents\\a.torrent``; a directory scan yields ``D:/Torrents/a.torrent``. Both are
the same file, and un-normalised they would produce two rows and download the torrent twice.
:func:`canonical_torrent_path` is therefore the only way a path is turned into a string here.

**The watched folder is age-limited, not just new-file-limited.** "Add every ``.torrent`` in this
folder" is a trap: the default folder is the user's *downloads* directory, which may hold
hundreds of torrents from years past, and importing them starts up to three downloads
immediately. :func:`find_new_torrents` only considers files modified within
:data:`MAX_TORRENT_AGE_DAYS`, which bounds the damage to the handful a user could plausibly have
added in the last few days, and still catches files that arrived while the app was closed.

**A file must stop changing before it is believed.** A ``.torrent`` dropped into a watched folder
is usually still being copied when the filesystem event fires, and a truncated ``.torrent`` parses
into an ``error`` row that the user has to delete. :class:`TorrentFolderWatcher` therefore
requires an unchanged size across two observations before reporting a file.

The watcher is a ``QFileSystemWatcher`` *plus* a periodic rescan, because the event alone is not
reliable: it is missed while the process is suspended, and a watcher cannot be attached to a
network drive. The rescan is also what picks up files that appeared while the app was not running.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Iterable, Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

log = logging.getLogger(__name__)

#: The extension that makes a local file a torrent candidate. Compared case-insensitively:
#: ``.TORRENT`` is a torrent and a case-sensitive test would silently drop it.
TORRENT_SUFFIX = ".torrent"

#: How recently a file must have been modified to be considered new. This is the guard that stops
#: a watched downloads folder from importing its whole history, and it is the reason a
#: file that arrived while the app was closed is still picked up on the next start.
MAX_TORRENT_AGE_DAYS = 3

#: Debounce after a filesystem event. Burst events (a multi-file copy, an unpack) coalesce into
#: one scan, and it also gives a slow copy time to land before the stability check looks at it.
WATCH_DEBOUNCE_MS = 1200

#: Safety-net rescan interval. A ``QFileSystemWatcher`` misses events across suspend/resume and
#: cannot watch a network drive at all, so this is what makes the feature dependable rather than
#: best-effort.
WATCH_RESCAN_MS = 60_000

#: A file must report the same size on two observations at least this far apart before it is
#: considered fully written.
WATCH_STABILITY_MS = 1200

#: Ceiling on files taken from one scan. The age limit already bounds the set, but a folder of
#: freshly-extracted torrents can be large, and adding hundreds of rows in one tick would stall
#: the GUI thread. The rest are picked up by the next rescan.
MAX_PER_SCAN = 50


def canonical_torrent_path(path: str) -> str:
    """Absolute, separator-normalised form of *path*, for use as a de-duplication key.

    ``abspath`` then ``normpath`` rather than ``resolve``: symlink resolution is a filesystem
    round trip per file and buys nothing here, since the goal is only that the *same* path spelled
    two ways produces one row. Case is deliberately preserved — ``normcase`` would lowercase the
    whole path on Windows, which then surfaces in the UI wherever the source is displayed.
    """
    try:
        return os.path.normpath(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return ""


def is_torrent_path(path: str) -> bool:
    """True when *path* is an existing regular file whose name ends in ``.torrent``.

    Existence is part of the test, not an extra: ``DownloadManager._detect_type`` only classifies
    a ``.torrent`` as a torrent when ``os.path.isfile`` agrees, so a path that does not exist
    would become an HTTP download. Reporting such a path as a candidate would be a lie the manager
    then acts on.
    """
    if not path or not isinstance(path, str):
        return False
    try:
        return (
            os.path.isfile(path)
            and os.path.splitext(path)[1].lower() == TORRENT_SUFFIX
        )
    except (OSError, ValueError):
        # A path with a NUL byte, or one the OS refuses to stat, is not a torrent.
        return False


def local_torrent_paths_from_mime(mime) -> list[str]:
    """Local ``.torrent`` files carried by a drag payload, canonicalised and de-duplicated.

    Deliberately narrow. Non-local URLs are skipped (a ``.torrent`` fetched over HTTP is already
    reachable through Add Download), and anything that is not a ``.torrent`` is skipped rather
    than reported, so the caller can accept a drag containing one torrent among other files
    without deciding what the others were meant to be.

    ``file://`` URLs must go through ``QUrl.toLocalFile()``: a raw ``file://`` string does not
    survive ``_detect_type``, which would quietly turn a dropped torrent into an HTTP download.
    """
    if mime is None:
        return []
    try:
        if not mime.hasUrls():
            return []
        urls = list(mime.urls())
    except Exception as exc:  # a malformed payload must not break the drag interaction
        log.debug("Could not read the drag payload: %s", exc)
        return []

    found: list[str] = []
    seen: set[str] = set()
    for url in urls:
        try:
            if not url.isLocalFile():
                continue
            local = url.toLocalFile()
        except Exception:
            continue
        if not local or not is_torrent_path(local):
            continue
        canonical = canonical_torrent_path(local)
        # Windows paths compare case-insensitively on the filesystem, so two spellings of one
        # name must not both be added. The key is case-folded; the stored path is not.
        key = os.path.normcase(canonical)
        if not canonical or key in seen:
            continue
        seen.add(key)
        found.append(canonical)
    return found


def find_new_torrents(
    folder: str,
    known: Optional[Iterable[str]] = None,
    *,
    max_age_days: int = MAX_TORRENT_AGE_DAYS,
    now: Optional[float] = None,
    limit: int = MAX_PER_SCAN,
) -> list[str]:
    """``.torrent`` files in *folder* that are recent enough to count as new.

    Sorted, so a scan is deterministic and the tests are order-independent. Non-recursive: a
    watch folder is one folder, and recursing into the payload directories a finished torrent
    leaves behind would be a way to import the wrong thing.

    *known* is skipped without touching the database, so a caller can exclude what it has already
    seen this session before paying for a single query per candidate.
    """
    if not folder or not os.path.isdir(folder):
        return []

    seen = {os.path.normcase(p) for p in (known or ())}
    cutoff = (time.time() if now is None else now) - max(0, int(max_age_days)) * 86400
    found: list[str] = []
    try:
        entries = list(os.scandir(folder))
    except OSError as exc:
        log.debug("Could not scan %s for torrents: %s", folder, exc)
        return []

    for entry in entries:
        if len(found) >= max(0, int(limit)):
            break
        try:
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(TORRENT_SUFFIX):
                continue
            if entry.stat().st_mtime < cutoff:
                continue  # older than the window: history, not a new arrival
        except OSError:
            # Vanished between listing and stat, or belongs to a user we cannot read. Either way
            # it is not something to add, and neither is a reason to abandon the rest of the scan.
            continue
        canonical = canonical_torrent_path(entry.path)
        key = os.path.normcase(canonical)
        if not canonical or key in seen:
            continue
        seen.add(key)
        found.append(canonical)

    return sorted(found)


class TorrentFolderWatcher(QObject):
    """Reports ``.torrent`` files that appear in a watched folder.

    Emits *candidate paths*; it does not add anything. Ingestion belongs to
    ``DownloadManager.add_torrent_files``, so the drop path, the watched folder and the Add
    Torrent button all apply one policy instead of three.

    A file is only emitted once its size has been unchanged across two observations at least
    :data:`WATCH_STABILITY_MS` apart, because a truncated ``.torrent`` parses into an ``error``
    row. The first sighting only records the size, which means a file that stops growing is
    reported by the next scan — at worst :data:`WATCH_RESCAN_MS` later, and immediately in the
    common case because the copy finishing generates another event.
    """

    #: Canonical paths of stable, recent ``.torrent`` files. Emitted in batches, never empty.
    torrents_found = Signal(list)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        debounce_ms: int = WATCH_DEBOUNCE_MS,
        rescan_ms: int = WATCH_RESCAN_MS,
        stability_ms: int = WATCH_STABILITY_MS,
        max_age_days: int = MAX_TORRENT_AGE_DAYS,
    ):
        super().__init__(parent)
        self._folder = ""
        self._enabled = False
        self._max_age_days = max(0, int(max_age_days))
        self._stability_ms = max(0, int(stability_ms))
        #: path -> (size, first seen monotonic). The stability ledger, nothing more.
        self._pending: dict[str, tuple[int, float]] = {}
        #: Canonical paths already emitted this session, so a later scan cannot re-report them.
        self._emitted: set[str] = set()
        #: Re-entrancy flag for :meth:`_scanning`.
        self._scanning_now = False

        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._on_directory_changed)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(max(0, int(debounce_ms)))
        self._debounce.timeout.connect(self.scan)

        self._rescan = QTimer(self)
        self._rescan.setInterval(max(1, int(rescan_ms)))
        self._rescan.timeout.connect(self.scan)

    # -- configuration -------------------------------------------------------------------------

    def folder(self) -> str:
        return self._folder

    def max_age_days(self) -> int:
        return self._max_age_days

    def set_max_age_days(self, days: int) -> None:
        """Update the maximum age threshold in days for scanned files."""
        self._max_age_days = max(0, int(days))

    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Arm or disarm the watcher. Idempotent, and safe before the app is running.

        The timers are only ever running while this returns True, so a watcher that is enabled in
        settings but never started by the manager cannot scan the user's disk — the same invariant
        ``DownloadManager._apply_backlog_timer_config`` protects for the backlog poll.
        """
        enabled = bool(enabled) and bool(self._folder)
        self._enabled = enabled
        if enabled:
            self._attach(self._folder)
            self._rescan.start()
        else:
            self._detach()
            self._rescan.stop()

    def set_folder(self, folder: str) -> None:
        """Point the watcher at *folder*, re-arming it if it is already enabled.

        A changed folder re-baselines: the ledger is cleared so files already sitting in the new
        folder are judged by the age rule rather than inheriting the previous folder's history.
        """
        folder = (folder or "").strip()
        if folder == self._folder:
            return
        self._folder = folder
        self._pending.clear()
        self._emitted.clear()
        if self._enabled:
            self._detach()
            if folder:
                self._attach(folder)

    def stop(self) -> None:
        """Stop everything. Never raises — it runs from ``DownloadManager.stop``."""
        self._enabled = False
        self._debounce.stop()
        self._rescan.stop()
        self._detach()

    # -- scanning -----------------------------------------------------------------------------

    def scan(self) -> list[str]:
        """Look for stable, recent ``.torrent`` files and emit the ones not seen before.

        Returns what it emitted, so a caller driving this directly (and the tests) do not have to
        race the signal. Wrapped end to end: this is a ``QTimer`` slot, and an exception escaping
        one silently kills the connection and leaves a watcher that never fires again.
        """
        with self._scanning() as entered:
            try:
                if not entered:
                    return []
                if not self._enabled or not self._folder:
                    return []
                found = self._stable_candidates()
                if found:
                    log.info(
                        "Watched folder %s yielded %d torrent(s)", self._folder, len(found)
                    )
                    self.torrents_found.emit(list(found))
                return found
            except Exception as exc:
                log.error("Error scanning the watched folder %s: %s", self._folder, exc)
                return []

    @contextmanager
    def _scanning(self):
        """Mark a scan in flight, yielding whether this call is the one that got there.

        ``scan`` is both a ``QTimer`` slot and callable by hand, and a manual call can land while
        the timer is mid-scan. The scan only touches this watcher's own dicts, so a re-entrant one
        would not corrupt anything — but it would emit the same file twice, and a duplicate
        torrent is a duplicate download.

        Always yields, including on the re-entrant path: a ``@contextmanager`` that returns without
        yielding raises ``RuntimeError: generator didn't yield`` on entry, which would turn a
        harmless re-entrant call into a crash inside the timer slot.
        """
        if self._scanning_now:
            yield False
            return
        self._scanning_now = True
        try:
            yield True
        finally:
            self._scanning_now = False

    def _stable_candidates(self, now: Optional[float] = None) -> list[str]:
        """Candidates whose size has settled, removing them from the ledger as it goes."""
        moment = time.monotonic() if now is None else now
        candidates = find_new_torrents(
            self._folder, self._emitted, max_age_days=self._max_age_days
        )
        stable: list[str] = []
        for path in candidates:
            try:
                size = os.path.getsize(path)
            except OSError:
                # Deleted between the scan and the stat; drop any ledger entry so a file that
                # reappears under the same name is judged afresh.
                self._pending.pop(path, None)
                continue

            key = os.path.normcase(path)
            first_size, first_seen = self._pending.get(path, (None, None))
            if first_size == size and (moment - first_seen) >= (self._stability_ms / 1000.0):
                # Unchanged for long enough: the copy has finished.
                stable.append(path)
                self._pending.pop(path, None)
                self._emitted.add(key)
            else:
                # First sighting, or still growing. Re-record; a file that grew resets the clock
                # so a slow copy cannot be declared stable halfway through.
                self._pending[path] = (size, moment if first_size != size else first_seen)

        # Forget ledger entries for files that have left the folder, so the dict cannot grow
        # without bound across a long session.
        if self._pending:
            current = {os.path.normcase(p) for p in candidates}
            for path in [p for p in self._pending if os.path.normcase(p) not in current]:
                self._pending.pop(path, None)
        return stable

    # -- wiring --------------------------------------------------------------------------------

    def _on_directory_changed(self, _path: str) -> None:
        # Restart rather than start: a burst of writes must produce one scan, not one per file.
        self._debounce.start()

    def _attach(self, folder: str) -> None:
        if not folder or self._folder in self._fs_watcher.directories():
            return
        if not os.path.isdir(folder):
            # Not an error worth surfacing: a download folder on a disconnected drive is ordinary,
            # and the next rescan re-attempts. The watcher's own poll keeps working if it ever
            # appears, and a wrong path simply yields nothing.
            log.debug("Not watching %s: not a directory", folder)
            return
        if not self._fs_watcher.addPath(folder):
            log.debug("Could not watch %s; falling back to periodic rescans", folder)

    def _detach(self) -> None:
        self._debounce.stop()
        watched = self._fs_watcher.directories()
        if watched:
            self._fs_watcher.removePaths(watched)
