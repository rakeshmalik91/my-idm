"""Clipboard capture: offer copied download URLs to the download manager.

A download manager that only receives URLs it was explicitly handed is missing the single
most common way a user encounters a link — they copy it out of a page, a chat client, or a
terminal. This module watches ``QClipboard.dataChanged``, debounces it, and hands anything that
looks like a list of downloadable URLs to ``DownloadManager.add_download()``.

Three decisions worth stating, because all of them are the difference between a feature people
keep and a feature people disable:

**The gate is all-or-nothing.** A copied text is accepted only when *every* non-blank line is
a downloadable URL. A chat message that happens to contain one link must not be captured, and
neither must the 200-line log tail someone just copied — otherwise the monitor fires on a
keystroke and the user stops trusting it. This is the same policy
``AddDownloadDialog._prefill_url`` already uses for its clipboard pre-fill, and the two share
:func:`looks_like_download_url` so they cannot drift.

**A URL has to prove it is a file before it becomes a row.** A copied link is text, and text
does not say what it points at. ``https://example.com/`` is indistinguishable from
``https://example.com/ubuntu.iso`` until somebody asks the server, so a URL copied out of a
browser address bar used to land in the table as a download that failed — the user then had to
find the row and delete it. :func:`decide_capture` resolves each candidate first (see
:mod:`my_idm.http_probe`) and drops anything that is not a file, is smaller than the configured
minimum, or names an ignored extension. The probe is what makes the size and extension settings
mean anything: neither can be evaluated from a URL that redirects to a named file, and a URL
with no extension has no extension to ignore. An unresolvable URL is dropped rather than
queued, which is the point — a row the user has to delete is worse than no row.

**The app's own clipboard writes are suppressed.** ``MainWindow._on_copy_url`` puts selected
rows' URLs on the clipboard, and re-reading that would re-add them. Deduping inside
``add_download`` is *not* enough: re-adding a paused download calls ``resume_download`` on it,
so pressing Ctrl+C on a paused row would silently start it. :meth:`ClipboardMonitor.suppress`
is therefore called by the writer, and the text it names is ignored on the next change.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from my_idm.http_probe import ProbeResult, is_probeable, probe_url

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


def extract_url_extension(url: str) -> str:
    """Extract lowercase file extension with leading dot from a URL, or empty string.

    Handles query parameters and fragments properly, e.g.
    'https://example.com/path/file.HTML?key=val#frag' -> '.html'
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        if not path:
            return ""
        name = Path(path).name
        if "." in name:
            _, ext = os.path.splitext(name)
            return ext.lower()
    except Exception:
        pass
    return ""


def is_ignored_extension(
    url: str, ignored_extensions: Optional[list[str] | set[str]] = None
) -> bool:
    """True when url's file extension matches one of the ignored extensions."""
    if not url or not ignored_extensions:
        return False
    ext = extract_url_extension(url)
    if not ext:
        return False
    for ign in ignored_extensions:
        ign_clean = ign.strip().lower()
        if not ign_clean:
            continue
        if not ign_clean.startswith("."):
            ign_clean = f".{ign_clean}"
        if ext == ign_clean:
            return True
    return False


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


def extract_download_urls(
    text: str,
    max_urls: int = DEFAULT_MAX_URLS,
    ignored_extensions: Optional[list[str] | set[str]] = None,
) -> list[str]:
    """Return the downloadable URLs in *text*, or ``[]`` unless every line qualifies.

    Blank lines are ignored. De-duplication is case-preserving and order-preserving: the same
    URL twice in one copy is one download, and ``add_download`` would resume the first anyway.
    URLs matching ``ignored_extensions`` (e.g. .html, .jpg) are filtered out.

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
        if is_ignored_extension(line, ignored_extensions):
            continue
        if len(urls) >= limit:
            break
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(line)
    return urls


#: Why a candidate URL was not captured. Surfaced to the user verbatim in the status bar: a
#: silent drop reads as a bug, and a drop the user cannot explain is a drop they stop
#: trusting.
REASON_UNREACHABLE = "could not be reached"
REASON_HTML_PAGE = "resolves to a web page, not a file"
REASON_TOO_SMALL = "smaller than the minimum capture size"
REASON_IGNORED_EXT = "extension is on the ignore list"


def decide_capture(
    url: str,
    probe: Optional[ProbeResult],
    *,
    min_bytes: int = 0,
    ignored_extensions: Optional[list[str] | set[str]] = None,
) -> tuple[bool, str]:
    """Should *url* be captured, given what the probe found? Returns (accept, reason).

    The extension check runs against **both** the URL as copied and the resolved target,
    because the two disagree exactly when it matters: ``/download?id=9`` has no extension to
    ignore but serves ``report.pdf``, and a shortener has a ``.html`` path that serves a
    ``.zip``. Either one can be the file the user actually meant.

    ``min_bytes`` of 0 disables the size check, matching the setting's "no minimum" wording. A
    probe that could not determine a size (``size == 0``) never fails this check: refusing
    every unsizeable response would reject every chunked and gzip-encoded download there is.

    An ``ok=False`` probe is a rejection. That is deliberately strict — it is what stops a
    copied link to a page that 404s or sits behind a login from becoming a permanent error
    row — but it does mean a capture whose host is briefly unreachable is dropped rather than
    retried. The user can always paste the URL into Add Download, which is not gated at all.
    """
    if not probe or not probe.ok:
        return False, REASON_UNREACHABLE

    if probe.is_html:
        return False, REASON_HTML_PAGE

    if is_ignored_extension(url, ignored_extensions) or is_ignored_extension(
        probe.filename, ignored_extensions
    ):
        return False, REASON_IGNORED_EXT

    if min_bytes > 0 and 0 < probe.size < min_bytes:
        return False, REASON_TOO_SMALL

    return True, ""


#: Ceiling on probes in flight for one copy. 20 URLs is a pasted list, and firing 20 requests
#: at an origin at the same moment is how a capture gets rate-limited into dropping everything.
MAX_CONCURRENT_PROBES = 4


def _run_coroutine_blocking(coro):
    """Drive *coro* to completion on a private loop and return its result.

    The default ``run_async``, and the one tests rely on to make ``capture_now()``'s return
    value meaningful. Production passes the manager's background loop instead, so the probe
    does not block the UI thread for its timeout.
    """
    return asyncio.run(coro)


async def resolve_candidates(
    urls: list[str],
    *,
    probe: Optional[Callable] = None,
    min_bytes: int = 0,
    ignored_extensions: Optional[list[str] | set[str]] = None,
    max_concurrent: int = MAX_CONCURRENT_PROBES,
) -> list[tuple[str, Optional[ProbeResult], str]]:
    """Resolve every URL and decide each one. Returns [(url, probe, reason)].

    All three elements are kept: the caller needs the ``ProbeResult`` to name the file
    ``add_download`` should save it as, and the empty ``reason`` marks an accepted URL.

    A URL with no HTTP transport — ``magnet:``, ``ftp:``, a local ``.torrent`` — is not probed
    and accepted as-is. There is no server to ask and no size to weigh, so a probe could only
    ever report failure and drop a link the manager handles perfectly well.
    """
    probe = probe or probe_url
    gate = asyncio.Semaphore(max(1, int(max_concurrent or MAX_CONCURRENT_PROBES)))
    limit = max(0, int(min_bytes or 0))

    async def one(url: str):
        if not is_probeable(url):
            return (url, None, "")
        async with gate:
            try:
                result = await probe(url)
            except Exception as exc:
                # A probe that raises is a probe that failed; the URL is not capturable. Swallowed
                # here rather than aborting the batch, so one bad host cannot cost the rest.
                log.info("Clipboard probe raised for %s: %s", url, exc)
                result = None
        accepted, reason = decide_capture(
            url, result, min_bytes=limit, ignored_extensions=ignored_extensions
        )
        return (url, result if accepted else None, "" if accepted else reason)

    if not urls:
        return []
    return list(await asyncio.gather(*(one(u) for u in urls)))


class ClipboardMonitor(QObject):
    """Watches the clipboard and captures text that is only downloadable URLs."""

    #: (urls, skipped) — ``skipped`` counts lines dropped by the configured maximum.
    urls_captured = Signal(list, int)
    #: [(url, reason), ...] for candidates the probe rejected. Emitted even when nothing was
    #: captured, because "I copied a link and nothing happened" is indistinguishable from a
    #: broken feature unless the app says why.
    urls_filtered = Signal(list)

    def __init__(
        self,
        manager,
        max_urls: int = DEFAULT_MAX_URLS,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: Optional[QObject] = None,
        queue_provider: Optional[Callable[[], str]] = None,
        min_file_size_kb: int = 1024,
        ignored_extensions: Optional[list[str]] = None,
        run_async: Optional[Callable] = None,
        probe: Optional[Callable] = None,
    ):
        super().__init__(parent)
        self._manager = manager
        self._max_urls = max(1, min(int(max_urls or DEFAULT_MAX_URLS), ABSOLUTE_MAX_URLS))
        self._debounce_ms = max(0, int(debounce_ms))
        self._min_file_size_kb = max(0, int(min_file_size_kb))
        self._ignored_extensions = list(ignored_extensions) if ignored_extensions is not None else []
        self._active = False
        # Injected rather than read off the manager so the monitor stays testable against a
        # fake manager that has no queue concept at all.
        self._queue_provider = queue_provider or (lambda: "")

        # Resolving a URL needs an event loop and an HTTP client, neither of which belongs in a
        # QObject. Both arrive as callables so this stays testable without a network: tests pass
        # a synchronous ``run_async`` and a scripted ``probe``, production passes the manager's
        # loop and the real probe. The default ``run_async`` drives the coroutine to completion
        # on the calling thread, which is correct for a caller that has no loop and is what
        # makes ``capture_now()``'s return value meaningful in tests.
        self._run_async = run_async or _run_coroutine_blocking
        self._probe = probe or probe_url

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

    @property
    def min_file_size_kb(self) -> int:
        return self._min_file_size_kb

    @property
    def ignored_extensions(self) -> list[str]:
        return list(self._ignored_extensions)

    @property
    def probe(self):
        """The async URL resolver. A public seam so a test can install one without a network."""
        return self._probe

    def set_probe(self, probe: Optional[Callable]) -> None:
        self._probe = probe or probe_url

    def set_max_urls(self, max_urls: int) -> None:
        self._max_urls = max(1, min(int(max_urls or DEFAULT_MAX_URLS), ABSOLUTE_MAX_URLS))

    def set_min_file_size_kb(self, size_kb: int) -> None:
        self._min_file_size_kb = max(0, int(size_kb))

    def set_ignored_extensions(self, exts: list[str]) -> None:
        self._ignored_extensions = list(exts) if exts is not None else []

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

        This is the debounce timer's slot. The clipboard read has to happen here, on the thread
        that owns ``QClipboard``, but resolving the URLs cannot: it is HTTP work, and blocking
        the GUI thread on it would freeze the window for the probe's whole timeout. So the
        candidates go to the injected ``run_async`` — the manager's background loop in
        production — and only the add happens back on the caller's thread.

        Returns the URLs handed to the manager, which means the return value is empty whenever
        ``run_async`` defers rather than completes. Tests use the blocking default so the value
        is real; nothing in the app reads it.
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

        urls = extract_download_urls(
            cleaned, self._max_urls, ignored_extensions=self._ignored_extensions
        )
        if not urls:
            return []
        # Counted against the cap, not against the returned list: a duplicate line was
        # collapsed, not skipped, and labelling that as "over the limit" would be a lie.
        non_blank = sum(1 for line in cleaned.splitlines() if line.strip())
        skipped = max(0, non_blank - self._max_urls)

        try:
            return self._run_async(self._capture(urls, skipped))
        except Exception as exc:
            # A failure here is a failed capture, not a failed app: the debounce timer must
            # never propagate out of its slot, or Qt tears the connection down and stops firing.
            log.warning("Clipboard capture aborted: %s", exc)
            return []

    async def _capture(self, urls: list[str], skipped: int) -> list[str]:
        """Resolve each candidate, then hand the survivors to the manager.

        Split out from :meth:`capture_now` so the whole decision is one awaitable unit that a
        test can drive directly, with no event loop and no network.
        """
        resolved = await resolve_candidates(
            urls,
            probe=self._probe,
            min_bytes=int(self._min_file_size_kb * 1024),
            ignored_extensions=self._ignored_extensions,
        )

        accepted = [(url, probe) for url, probe, reason in resolved if not reason]
        filtered = [(url, reason) for url, probe, reason in resolved if reason]
        if filtered:
            # Emitted whether or not anything survived, so the status bar can explain the silence.
            self.urls_filtered.emit(filtered)

        added: list[str] = []
        # A copied URL is a foreground action like Ctrl+V, so it follows whichever queue the
        # view is scoped to. Browser captures deliberately do not - see
        # DownloadManager.add_download_from_browser.
        queue_id = self._queue_provider()
        for url, probe in accepted:
            try:
                metadata = {"capture_source": "clipboard"}
                if self._min_file_size_kb > 0:
                    # Kept even when the probe already sized the file: the engine probes again
                    # authoritatively, and a URL that shrank in between must still be refused.
                    metadata["pending_min_bytes"] = int(self._min_file_size_kb * 1024)
                # The probe followed redirects and read Content-Disposition, so it often knows the
                # real filename where the copied URL showed none. Passing it as an explicit
                # filename stops the row appearing nameless until the engine's own probe lands.
                filename = (probe.filename if probe else "") or ""
                did = self._manager.add_download(
                    url, queue_id=queue_id, metadata=metadata, filename=filename
                )
            except Exception as exc:
                log.warning("Clipboard capture failed for %s: %s", url, exc)
                continue
            if did:
                added.append(url)
        if added:
            log.info("Clipboard capture added %d download(s)", len(added))
            self.urls_captured.emit(list(added), skipped)
        return added