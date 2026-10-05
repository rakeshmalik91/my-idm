"""Ask a server what a URL actually is, before deciding to download it.

Both capture paths need the same answer to the same question - *is this URL a file, and how
big is it?* - and getting it costs one request. ``BrowserServer`` already had a probe of its
own for exactly this; this module is that probe with the reusable parts pulled out, so the
clipboard monitor can resolve a copied link to a real file **before** it becomes a row.

Two things make the answer worth more than a Content-Length:

* **Redirects are followed.** ``https://shortener/abc`` and a CDN link both resolve to
  something else entirely, and it is the *final* URL whose filename and content type describe
  the file. The copied URL is often a bare origin with no extension at all.
* **Content-Disposition is honoured.** ``/download?id=9`` names its file in a header, so the
  extension a filter needs is frequently invisible in the URL.

The probe is deliberately cheap and deliberately pessimistic: one HEAD, and only if that comes
back without a usable size one single-byte ranged GET. It is a *filter*, not the download -
``HTTPEngine`` still probes authoritatively before transferring a byte, so a wrong answer here
costs one round trip rather than a corrupt file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import unquote, urlparse

import aiohttp

log = logging.getLogger("my_idm")

#: Schemes with an HTTP transport, and therefore a server to ask. Anything else - ``magnet:``,
#: a local ``.torrent`` path, ``ftp:`` - has no Content-Length to read, so probing it is
#: pointless; callers capture those on sight.
PROBEABLE_SCHEMES = ("http", "https")

#: Total budget for the probe. Short on purpose: this runs while the user is still looking at
#: what they copied, and a capture that takes four seconds to appear feels broken rather than
#: careful. A URL this slow to answer is one the real download will also struggle with.
PROBE_TIMEOUT_S = 1.8

#: Subset of :data:`PROBE_TIMEOUT_S` allowed for the TCP/TLS handshake, so an unroutable host
#: fails fast instead of consuming the whole budget on connect.
PROBE_CONNECT_TIMEOUT_S = 1.0

#: Many origins answer a bare ``python-requests``/``aiohttp`` HEAD with 403 or 405 purely on the
#: User-Agent, which would make this probe report "unreachable" for perfectly downloadable files.
PROBE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def is_probeable(url: str) -> bool:
    """True when *url* has an HTTP server that can be asked about the file.

    Scheme-only test, like :func:`my_idm.browser_server.is_capturable_url` and for the same
    reason: nothing short of a request can settle whether a host serves the file.
    """
    if not url or not isinstance(url, str):
        return False
    scheme = url.split("://", 1)[0].split(":", 1)[0].strip().lower()
    return scheme in PROBEABLE_SCHEMES


def filename_from_headers(
    headers: Optional[Mapping[str, str]], final_url: str = ""
) -> str:
    """Best available filename for a response: ``Content-Disposition``, then the final URL.

    RFC 5987's ``filename*=UTF-8''name.zip`` is preferred over plain ``filename=`` because it
    survives non-ASCII names that the quoted form is not allowed to carry.
    """
    filename = ""
    cd = ""
    if headers:
        try:
            cd = headers.get("Content-Disposition", "") or ""
        except Exception:
            cd = ""
    if cd:
        if "filename*=" in cd:
            part = cd.split("filename*=")[-1].split(";")[0].strip().strip('"\'')
            if "''" in part:
                _, _, encoded = part.partition("''")
                filename = unquote(encoded)
            else:
                filename = unquote(part)
        elif "filename=" in cd:
            part = cd.split("filename=")[-1].split(";")[0].strip().strip('"\'')
            filename = unquote(part)
        # A server can offer both, and the quoted one is usually a transliteration fallback
        # ("r%C3%A9sum%C3%A9.pdf" vs "resume.pdf"). Prefer whichever carries a real extension.
        if filename and "." not in filename and "filename=" in cd:
            fallback = cd.split("filename=")[-1].split(";")[0].strip().strip('"\'')
            fallback = unquote(fallback)
            if fallback and "." in fallback:
                filename = fallback

    if not filename and final_url:
        try:
            parsed = urlparse(final_url)
            path = unquote(parsed.path)
            name = Path(path).name
            if name and "." in name:
                filename = name
            else:
                filename = name
        except Exception:
            filename = ""
    return filename


@dataclass(frozen=True)
class ProbeResult:
    """What one probe learned about a URL. Every field is best-effort.

    ``size`` of 0 means *unknown*, never *empty*: a chunked response, a HEAD the origin
    refuses, or a probe that timed out all land there, and the three are not interchangeable
    for a caller deciding whether to download. ``ok`` is the separate question of whether the
    server answered at all - it is False for a transport failure and for any status >= 400.
    """

    url: str
    ok: bool = False
    size: int = 0
    content_type: str = ""
    filename: str = ""
    final_url: str = ""
    status: int = 0
    error: str = ""

    @property
    def is_html(self) -> bool:
        """True when the server described the response as an HTML document."""
        return "text/html" in self.content_type.lower()

    @property
    def resolved_url(self) -> str:
        """The URL after redirects, falling back to the one that was probed."""
        return self.final_url or self.url


async def _size_from_headers(
    resp, *, ranged: bool, final_url: str
) -> tuple[int, str, str, int]:
    """Pull (size, content_type, filename, status) out of a response.

    ``Content-Range`` outranks ``Content-Length`` for a ranged GET: a ``206`` reports the
    length of the *slice* (1 byte, here) in Content-Length, and only Content-Range carries the
    size of the whole file. Taking Content-Length there would size every server that honours
    Range at 1 byte and fail every minimum-size filter in the app.
    """
    status = int(getattr(resp, "status", 0) or 0)
    headers = getattr(resp, "headers", None) or {}
    content_type = ""
    size = 0
    try:
        content_type = headers.get("Content-Type", "") or ""
    except Exception:
        content_type = ""
    try:
        if ranged:
            cr = headers.get("Content-Range", "")
            if cr and "/" in cr:
                total = cr.split("/")[-1].strip()
                if total.isdigit() and int(total) > 0:
                    size = int(total)
        if size <= 0:
            cl = headers.get("Content-Length", "")
            # A ranged GET that was answered 200 (i.e. Range ignored) still reports the full
            # length; a 206 would report the slice, which Content-Range above already covered.
            if cl and cl.isdigit() and int(cl) > 0 and (not ranged or status == 200):
                size = int(cl)
    except Exception:
        size = 0
    filename = filename_from_headers(headers, final_url or getattr(resp, "url", "") or "")
    return size, content_type, filename, status


def _response_url(resp, fallback: str) -> str:
    """The URL a response ended at, i.e. the post-redirect one.

    Read defensively: it is an enrichment (it tells us the real filename) and the fallback is
    the URL we already asked for, so a response object that cannot answer costs nothing.
    """
    try:
        return str(getattr(resp, "url", "") or "") or fallback
    except Exception:
        return fallback


def html_is_a_file(url: str) -> bool:
    """Whether a ``text/html`` response from *url* is being counted as a downloadable file.

    True only for a URL that openly names itself HTML. An origin serving ``text/html`` for
    ``/api/export`` is answering a browser, not offering a file, and its Content-Length is the
    size of a page rather than the size of a download.

    Shared rather than duplicated: the browser and clipboard capture paths both size-against
    this, and they have to agree or a URL behaves differently depending on how it arrived.
    """
    return url.lower().split("?", 1)[0].split("#", 1)[0].endswith((".htm", ".html"))


async def probe_url(
    url: str,
    headers: Optional[Mapping[str, str]] = None,
    *,
    timeout: float = PROBE_TIMEOUT_S,
    connect_timeout: float = PROBE_CONNECT_TIMEOUT_S,
    accept_html: Optional[bool] = None,
) -> ProbeResult:
    """Resolve *url* to the file it serves. Never raises; failure is ``ok=False``.

    Two attempts, because each covers the other's blind spot. HEAD is the cheap one and gets
    the length from an origin that sends it, but a fair number of CDNs and PHP handlers answer
    HEAD with 405 or omit Content-Length entirely. A ranged GET for a single byte gets the real
    total out of ``Content-Range`` from those, and costs one byte on the ones that did not.

    ``accept_html`` decides whether a ``text/html`` response is allowed to contribute its size.
    It defaults to "only if the URL names itself HTML", which is the conservative reading: the
    size of a web page is not a download size, and reporting it as one is what let page URLs
    through a minimum-size filter before this module existed.
    """
    if not is_probeable(url):
        return ProbeResult(url=url, ok=False, error="not an HTTP URL")

    allow_html = html_is_a_file(url) if accept_html is None else bool(accept_html)
    req_headers = {"User-Agent": PROBE_USER_AGENT}
    if headers:
        try:
            req_headers.update({k: v for k, v in headers.items() if v is not None})
        except Exception:
            pass

    timeout_cfg = aiohttp.ClientTimeout(total=timeout, connect=connect_timeout)
    best = ProbeResult(url=url, ok=False, error="no response")

    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            try:
                async with session.head(
                    url, headers=req_headers, allow_redirects=True
                ) as resp:
                    final = _response_url(resp, url)
                    size, ctype, fname, status = await _size_from_headers(
                        resp, ranged=False, final_url=final
                    )
                    if status < 400:
                        best = ProbeResult(
                            url=url,
                            ok=True,
                            size=size if (allow_html or "text/html" not in ctype.lower()) else 0,
                            content_type=ctype,
                            filename=fname,
                            final_url=final,
                            status=status,
                        )
                    else:
                        best = ProbeResult(
                            url=url,
                            ok=False,
                            content_type=ctype,
                            filename=fname,
                            final_url=final,
                            status=status,
                            error=f"HTTP {status}",
                        )
            except Exception as exc:
                log.debug("HEAD probe failed for %s: %s", url, exc)
                best = ProbeResult(url=url, ok=False, error=str(exc) or "HEAD failed")

            # Only pay for the second request when the first did not produce a usable size.
            if best.ok and best.size > 0:
                return best

            get_headers = dict(req_headers)
            get_headers["Range"] = "bytes=0-0"
            try:
                async with session.get(
                    url, headers=get_headers, allow_redirects=True
                ) as resp:
                    final = _response_url(resp, url)
                    size, ctype, fname, status = await _size_from_headers(
                        resp, ranged=True, final_url=final
                    )
                    if status < 400:
                        # A HEAD that failed outright must not poison a GET that worked: a 405
                        # on HEAD is a routine origin quirk, not an unreachable file.
                        best = ProbeResult(
                            url=url,
                            ok=True,
                            size=size if (allow_html or "text/html" not in ctype.lower()) else 0,
                            content_type=ctype,
                            filename=fname,
                            final_url=final,
                            status=status,
                        )
                    elif not best.ok:
                        best = ProbeResult(
                            url=url,
                            ok=False,
                            content_type=ctype,
                            filename=fname,
                            final_url=final,
                            status=status,
                            error=f"HTTP {status}",
                        )
            except Exception as exc:
                log.debug("Ranged GET probe failed for %s: %s", url, exc)
                if not best.ok:
                    best = ProbeResult(url=url, ok=False, error=str(exc) or "GET failed")
    except Exception as exc:
        log.debug("Probe session failed for %s: %s", url, exc)
        return ProbeResult(url=url, ok=False, error=str(exc) or "probe failed")

    return best