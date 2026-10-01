"""Embedded loopback REST server for Chrome extension integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING, Union
import aiohttp
from aiohttp import web

from my_idm.config import BrowserIntegrationConfig

if TYPE_CHECKING:
    from my_idm.manager import DownloadManager

log = logging.getLogger(__name__)

#: URL schemes an external downloader can actually fetch. Everything else the browser can
#: hand us - ``blob:`` (a JS-generated in-memory object), ``data:`` (inline payload),
#: ``file:``, ``javascript:``, ``about:``, ``chrome-extension:`` - has no HTTP transport
#: outside the page that created it.
CAPTURABLE_SCHEMES = ("http", "https", "magnet")


def is_capturable_url(url: str) -> bool:
    """True when *url* is a scheme My-IDM can fetch on its own.

    Scheme-only test, deliberately: a host cannot be validated without a request, and the
    whole point here is to reject the URLs that provably cannot work *before* spending a
    retry ladder on them. Case-insensitive because ``BLOB:`` and ``HtTpS://`` are equally
    valid to a browser.
    """
    if not url or not isinstance(url, str):
        return False
    scheme = url.split("://", 1)[0].split(":", 1)[0].strip().lower()
    return scheme in CAPTURABLE_SCHEMES


class BrowserServer:
    """Runs a local aiohttp REST server on 127.0.0.1:19582 to receive downloads from the browser extension."""

    def __init__(self, manager: DownloadManager, config: Optional[BrowserIntegrationConfig] = None):
        self._manager = manager
        self._config = config or BrowserIntegrationConfig.load()
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def config(self) -> BrowserIntegrationConfig:
        return self._config

    def set_config(self, config: BrowserIntegrationConfig):
        self._config = config

    def _cors_headers(self) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
        }

    async def _handle_options(self, request: web.Request) -> web.Response:
        return web.Response(status=200, headers=self._cors_headers())

    async def _handle_health(self, request: web.Request) -> web.Response:
        active_count = 0
        try:
            active_count = self._manager._get_active_download_count()
        except Exception:
            pass
        data = {
            "status": "ok",
            "app": "My-IDM",
            "version": "1.0.0",
            "active_downloads": active_count,
        }
        return web.json_response(data, headers=self._cors_headers())

    async def _handle_config(self, request: web.Request) -> web.Response:
        data = {
            "status": "ok",
            "enabled": self._config.enabled,
            "port": self._config.port,
            # snake_case canonical
            "intercept_all": self._config.intercept_all,
            "intercept_torrent_files": self._config.intercept_torrent_files,
            "intercept_magnet_links": self._config.intercept_magnet_links,
            "min_file_size_kb": self._config.min_file_size_kb,
            "bypassed_extensions": self._config.bypassed_extensions,
            # camelCase aliases for direct JS extension access
            "interceptDownloads": self._config.intercept_all,
            "interceptTorrentFiles": self._config.intercept_torrent_files,
            "interceptMagnetLinks": self._config.intercept_magnet_links,
            "minFileSizeKb": self._config.min_file_size_kb,
            "bypassExtensions": self._config.bypassed_extensions,
        }
        return web.json_response(data, headers=self._cors_headers())

    async def _probe_content_length(
        self, url: str, headers: Optional[dict] = None, cookies: str = ""
    ) -> Optional[int]:
        """Perform a fast HEAD or range probe to determine file size in bytes."""
        try:
            req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            if headers and isinstance(headers, dict):
                req_headers.update(headers)
            if cookies:
                req_headers["Cookie"] = cookies

            timeout = aiohttp.ClientTimeout(total=1.8, connect=1.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1. Try fast HEAD request
                try:
                    async with session.head(url, headers=req_headers, allow_redirects=True) as resp:
                        if resp.status < 400:
                            ct = resp.headers.get("Content-Type", "").lower()
                            if "text/html" not in ct or url.lower().endswith((".htm", ".html")):
                                cl = resp.headers.get("Content-Length")
                                if cl and cl.isdigit() and int(cl) > 0:
                                    return int(cl)
                except Exception:
                    pass

                # 2. Try fast GET with byte range (bytes=0-0)
                req_headers["Range"] = "bytes=0-0"
                try:
                    async with session.get(url, headers=req_headers, allow_redirects=True) as resp:
                        if resp.status < 400:
                            ct = resp.headers.get("Content-Type", "").lower()
                            if "text/html" not in ct or url.lower().endswith((".htm", ".html")):
                                cr = resp.headers.get("Content-Range")
                                if cr and "/" in cr:
                                    total_str = cr.split("/")[-1].strip()
                                    if total_str.isdigit() and int(total_str) > 0:
                                        return int(total_str)
                                cl = resp.headers.get("Content-Length")
                                if cl and cl.isdigit() and resp.status == 200 and int(cl) > 0:
                                    return int(cl)
                except Exception:
                    pass
        except Exception as exc:
            log.debug("Probing size failed for %s: %s", url, exc)
        return None

    async def _handle_add(self, request: web.Request) -> web.Response:
        if not self._config.enabled:
            return web.json_response(
                {"status": "error", "message": "Browser integration is disabled in My-IDM settings."},
                status=403,
                headers=self._cors_headers(),
            )
        if not self._config.intercept_all:
            # Capture is paused - by the tray toggle or the global hotkey. Answering 200 with
            # `ignored` rather than an error is deliberate: the extension treats a non-ok
            # status as "My-IDM is broken" and falls back to a browser download, whereas
            # `ignored` is the shape it already understands for "handled, not queued".
            return web.json_response(
                {
                    "status": "ignored",
                    "reason": "capture_paused",
                    "message": "Download capture is paused in My-IDM.",
                },
                headers=self._cors_headers(),
            )
        try:
            body = await request.json()
        except Exception as e:
            return web.json_response(
                {"status": "error", "message": f"Invalid JSON payload: {e}"},
                status=400,
                headers=self._cors_headers(),
            )

        url = body.get("url", "").strip()
        if not url:
            return web.json_response(
                {"status": "error", "message": "Missing required 'url' parameter."},
                status=400,
                headers=self._cors_headers(),
            )

        # Reject schemes that have no HTTP transport before anything is queued. A
        # `blob:` URL (what Chrome reports for JS-generated downloads, and what GitHub
        # hands out on some private-repo asset pages) is a browser-internal object
        # reference: there is nothing for an external downloader to fetch. Queuing one
        # only to fail means the user waits out the full retry ladder - 5s, 10s, 20s, 40s,
        # 60s - for a download that can never start. `data:` is the same class of thing.
        if not is_capturable_url(url):
            return web.json_response(
                {
                    "status": "ignored",
                    "reason": "unsupported_url_scheme",
                    "message": (
                        f"Cannot capture '{url[:80]}': only http, https and magnet URLs can "
                        "be downloaded outside the browser. This page handed us a "
                        "browser-internal URL, so the browser should download it natively."
                    ),
                },
                status=200,
                headers=self._cors_headers(),
            )

        filename = body.get("filename", "").strip()
        save_path = body.get("save_path", "").strip()
        cookies = body.get("cookies", "")
        referrer = body.get("referrer", "").strip()
        user_agent = body.get("user_agent", "").strip()
        extra_headers = body.get("headers", {})

        # Check minimum file size
        raw_size = body.get("total_bytes", 0) or body.get("file_size", 0)
        try:
            total_bytes = int(raw_size)
        except (ValueError, TypeError):
            total_bytes = 0

        min_bytes = self._config.min_file_size_kb * 1024
        pending_min_bytes = 0
        if self._config.min_file_size_kb > 0 and not url.startswith("magnet:"):
            # If size was not provided by browser extension, do a fast probe
            if total_bytes <= 0:
                try:
                    probe_headers = extra_headers if isinstance(extra_headers, dict) else {}
                    if referrer:
                        probe_headers["Referer"] = referrer
                    if user_agent:
                        probe_headers["User-Agent"] = user_agent
                    probed = await self._probe_content_length(url, probe_headers, cookies=cookies)
                    if probed is not None and probed > 0:
                        total_bytes = probed
                except Exception as probe_err:
                    log.debug("Size probe error for %s: %s", url, probe_err)

            if total_bytes > 0 and total_bytes < min_bytes:
                log.info(
                    "Skipping browser download '%s' (%d bytes < min threshold %d bytes / %d KB)",
                    filename or url,
                    total_bytes,
                    min_bytes,
                    self._config.min_file_size_kb,
                )
                return web.json_response(
                    {
                        "status": "ignored",
                        "reason": "file_size_below_minimum",
                        "message": f"File size ({total_bytes} bytes) is below minimum threshold ({min_bytes} bytes).",
                    },
                    status=200,
                    headers=self._cors_headers(),
                )
            if total_bytes <= 0:
                # The size is still unknown, so the threshold cannot be applied here.
                # Do NOT simply accept it: the `total_bytes > 0` guard above means an
                # unsizeable URL walked straight past a configured minimum, which is how a
                # sub-threshold file kept getting captured. Hand the threshold to the
                # engine instead - it probes every download before transferring a byte, so
                # it has the authoritative size and can refuse with a real reason.
                log.info(
                    "Size unknown for browser download '%s'; deferring the %d KB minimum "
                    "to the engine's own probe",
                    filename or url, self._config.min_file_size_kb,
                )
                pending_min_bytes = min_bytes

        try:
            download_id = self._manager.add_download_from_browser(
                url=url,
                filename=filename,
                save_path=save_path,
                headers=extra_headers if isinstance(extra_headers, dict) else {},
                cookies=cookies,
                referrer=referrer,
                user_agent=user_agent,
                pending_min_bytes=pending_min_bytes,
            )
            return web.json_response(
                {"status": "ok", "id": download_id},
                headers=self._cors_headers(),
            )
        except Exception as exc:
            log.exception("Error adding download from browser: %s", exc)
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=500,
                headers=self._cors_headers(),
            )

    async def start(self) -> bool:
        """Start the HTTP REST server on host:port."""
        if self._running:
            return True
        try:
            self._app = web.Application()
            self._app.router.add_route("OPTIONS", "/{tail:.*}", self._handle_options)
            self._app.router.add_get("/", self._handle_health)
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/config", self._handle_config)
            self._app.router.add_post("/add", self._handle_add)

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._config.host, self._config.port)
            await self._site.start()
            self._running = True
            log.info("Browser integration server started at http://%s:%d", self._config.host, self._config.port)
            return True
        except Exception as e:
            log.warning("Failed to start browser integration server on %s:%d: %s", self._config.host, self._config.port, e)
            self._running = False
            if self._runner:
                try:
                    await self._runner.cleanup()
                except Exception:
                    pass
                self._runner = None
            return False

    async def stop(self):
        """Stop the HTTP REST server cleanly."""
        if not self._running:
            return
        self._running = False
        if self._site:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None
        log.info("Browser integration server stopped")
