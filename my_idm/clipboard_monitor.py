"""Clipboard capture: offer copied download URLs to the download manager.

A download manager that only receives URLs it was explicitly handed is missing the single
most common way a user encounters a link — they copy it out of a page, a chat client, or a
terminal. This module watches ``QClipboard.dataChanged``, debounces it, and hands anything that
looks like a list of downloadable URLs to ``DownloadManager.add_download()``.

Two decisions worth stating, because both are the difference between a feature people keep
and a feature people disable:

**The gate is all-or-nothing.** A copied text is accepted only when *every* non-blank line is
a downloadable URL. A chat message that happens to contain one link must not be captured, and
neither must the 200-line log tail someone just copied — otherwise the monitor fires on a
keystroke and the user stops trusting it. This is the same policy
``AddDownloadDialog._prefill_url`` already uses for its clipboard pre-fill, and the two share
:func:`looks_like_download_url` so they cannot drift.

**The app's own clipboard writes are suppressed.** ``MainWindow._on_copy_url`` puts selected
rows' URLs on the clipboard, and re-reading that would re-add them. Deduping inside
``add_download`` is *not* enough: re-adding a paused download calls ``resume_download`` on it,
so pressing Ctrl+C on a paused row would silently start it. :meth:`ClipboardMonitor.suppress`
is therefore called by the writer, and the text it names is ignored on the next change.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

log = logging.getLogger("my_idm")

DEFAULT_DEBOUNCE_MS = 500
DEFAULT_MAX_URLS = 20

#: Hard ceiling on one copy event, independent of the configured maximum. A user who pastes a
#: generated list of ten thousand URLs must not get ten thousand rows inserted in one go.
ABSOLUTE_MAX_URLS = 200

_MAX_URL_LENGTH = 4096

#: Ceiling on pending self-write suppressions. Reaching it means a writer has named text that
#: never appeared on the clipboard, which is worth dropping the oldest half over.
_MAX_SUPPRESSED_TEXTS = 32

#: Schemes ``add_download`` can act on. ``ftp://`` and a local ``.torrent`` path are included
#: because the manager handles both; anything else is somebody else's object reference.
_URL_PREFIXES = ("http://", "https://", "ftp://", "magnet:?")
_TORRENT_SUFFIX = ".torrent"


def looks_like_download_url(text: str) -> bool:
    """True when *text* is something ``DownloadManager.add_download`` can act on.

    Shares its notion of a downloadable URL with ``AddDownloadDialog._prefill_url``, which
    delegates here.
    """
    if not text or len(text) > _MAX_URL_LENGTH:
        return False
    trimmed = text.strip()
    if not trimmed:
        return False
    lower = trimmed.lower()
    if lower.startswith(_URL_PREFIXES):
        return True
    if lower.endswith(_TORRENT_SUFFIX):
        # A path that really exists on disk, or an explicit file:// reference. A bare string
        # that merely ends in .torrent is not a download the manager can fetch.
        return lower.startswith("file://") or os.path.isfile(trimmed)
    return False


def extract_download_urls(text: str, max_urls: int = DEFAULT_MAX_URLS) -> list[str]:
    """Return the downloadable URLs in *text*, or ``[]`` unless every line qualifies.

    Blank lines are ignored. De-duplication is case-preserving and order-preserving: the same
    URL twice in one copy is one download, and ``add_download`` would resume the first anyway.

    The per-line length cap lives in :func:`looks_like_download_url`, deliberately *not* here:
    a cap on the whole payload would refuse a legitimate list of fifty links, which is exactly
    the case ``max_urls`` exists to handle.
    """
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    if not all(looks_like_download_url(line) for line in lines):
        return []
    limit = max(1, min(int(max_urls or DEFAULT_MAX_URLS), ABSOLUTE_MAX_URLS))
    if len(lines) > limit:
        log.info(
            "Clipboard capture capped at %d of %d URLs", limit, len(lines),
        )
    seen: set[str] = set()
    urls: list[str] = []
    for line in lines:
        if len(urls) >= limit:
            break
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(line)
    return urls


class ClipboardMonitor(QObject):
    """Watches the clipboard and captures text that is only downloadable URLs."""

    #: (urls, skipped) — ``skipped`` counts lines dropped by the configured maximum.
    urls_captured = Signal(list, int)

    def __init__(
        self,
        manager,
        max_urls: int = DEFAULT_MAX_URLS,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: Optional[QObject] = None,
        queue_provider: Optional[Callable[[], str]] = None,
    ):
        super().__init__(parent)
        self._manager = manager
        self._max_urls = max(1, min(int(max_urls or DEFAULT_MAX_URLS), ABSOLUTE_MAX_URLS))
        self._debounce_ms = max(0, int(debounce_ms))
        self._active = False
        # Injected rather than read off the manager so the monitor stays testable against a
        # fake manager that has no queue concept at all.
        self._queue_provider = queue_provider or (lambda: "")

        # ``setSingleShot`` and a restart on every change is what makes this a debounce rather
        # than a poll: a burst of clipboard writes coalesces into one read.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self._debounce_ms)
        self._debounce.timeout.connect(self.capture_now)

        #: Exact clipboard text to ignore on the next change, written by whoever put it there.
        #: Capped because every entry is consumed by one clipboard change, and a writer that
        #: never produces a matching change would otherwise grow this without bound.
        self._suppressed_texts: list[str] = []

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def max_urls(self) -> int:
        return self._max_urls

    def start(self) -> None:
        """Begin watching. Idempotent."""
        if self._active:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            # QApplication.clipboard() can legitimately return None (see tests/test_main_window.py),
            # in which case there is nothing to subscribe to and the feature stays inert.
            log.warning("Clipboard monitor requested but QClipboard is unavailable")
            return
        try:
            clipboard.dataChanged.connect(self._on_data_changed)
        except Exception as exc:
            log.warning("Could not subscribe to clipboard changes: %s", exc)
            return
        self._active = True
        log.info("Clipboard capture enabled (max %d URLs per copy)", self._max_urls)

    def stop(self) -> None:
        """Stop watching. Idempotent, and safe to call without a matching :meth:`start`.

        Never raises. This runs from ``MainWindow.closeEvent``, where an exception would abort
        teardown and leave the rest of the window's cleanup undone.
        """
        self._debounce.stop()
        if not self._active:
            return
        self._active = False
        try:
            clipboard = QGuiApplication.clipboard()
            if clipboard is None:
                return
            clipboard.dataChanged.disconnect(self._on_data_changed)
        except Exception as exc:
            log.debug("Clipboard disconnect raised (harmless): %s", exc)

    def suppress(self, text: str) -> None:
        """Ignore *text* the next time it appears on the clipboard.

        Called by any code that writes to the clipboard on the user's behalf, so that copying
        a download's URL does not re-add it. The suppression is consumed by the first change
        that carries this exact text and nothing else — a subsequent genuine copy of the same
        URL captures normally.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if len(self._suppressed_texts) >= _MAX_SUPPRESSED_TEXTS:
            del self._suppressed_texts[: len(self._suppressed_texts) // 2]
        self._suppressed_texts.append(cleaned)

    def _on_data_changed(self) -> None:
        # Restart rather than start, so N writes inside the window produce one read.
        self._debounce.start()

    def capture_now(self) -> list[str]:
        """Read the clipboard once and add anything captureable.

        This is the debounce timer's slot, and the seam tests drive: no event loop and no real
        clipboard round-trip required. Returns the URLs that were handed to the manager.
        """
        if not self._debounce_ms:
            self._debounce.stop()
        try:
            clipboard = QGuiApplication.clipboard()
        except Exception as exc:
            log.debug("Could not reach the clipboard: %s", exc)
            return []
        if clipboard is None:
            return []
        try:
            text = clipboard.text() or ""
        except Exception as exc:
            log.debug("Could not read clipboard text: %s", exc)
            return []

        cleaned = text.strip()
        if not cleaned:
            return []
        if cleaned in self._suppressed_texts:
            self._suppressed_texts.remove(cleaned)
            log.debug("Ignoring clipboard text this app just wrote")
            return []

        urls = extract_download_urls(cleaned, self._max_urls)
        if not urls:
            return []
        # Counted against the cap, not against the returned list: a duplicate line was
        # collapsed, not skipped, and labelling that as "over the limit" would be a lie.
        non_blank = sum(1 for line in cleaned.splitlines() if line.strip())
        skipped = max(0, non_blank - self._max_urls)

        added: list[str] = []
        # A copied URL is a foreground action like Ctrl+V, so it follows whichever queue the
        # view is scoped to. Browser captures deliberately do not - see
        # DownloadManager.add_download_from_browser.
        queue_id = self._queue_provider()
        for url in urls:
            try:
                did = self._manager.add_download(url, queue_id=queue_id)
            except Exception as exc:
                log.warning("Clipboard capture failed for %s: %s", url, exc)
                continue
            if did:
                added.append(url)
        if added:
            log.info("Clipboard capture added %d download(s)", len(added))
            self.urls_captured.emit(list(added), skipped)
        return added