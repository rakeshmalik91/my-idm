"""Embedded loopback REST server for Chrome extension integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING, Union
import aiohttp
from aiohttp import web

from my_idm.config import BrowserIntegrationConfig
from my_idm.http_probe import html_is_a_file, probe_url

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
        return web.json_response(self._config_payload(), headers=self._cors_headers())

    def _config_payload(self) -> dict:
        """The active config in both casings, so neither side has to know the other's."""
        return {
            "status": "ok",
            "enabled": self._config.enabled,
            "port": self._config.port,
            # snake_case canonical
            "intercept_all": self._config.intercept_all,
            "intercept_torrent_files": self._config.intercept_torrent_files,
            "intercept_magnet_links": self._config.intercept_magnet_links,
            "min_file_size_kb": self._config.min_file_size_kb,
            "skip_unknown_size_downloads": self._config.skip_unknown_size_downloads,
            "bypassed_extensions": self._config.bypassed_extensions,
            # camelCase aliases for direct JS extension access
            "interceptDownloads": self._config.intercept_all,
            "interceptTorrentFiles": self._config.intercept_torrent_files,
            "interceptMagnetLinks": self._config.intercept_magnet_links,
            "minFileSizeKb": self._config.min_file_size_kb,
            "skipUnknownSizeDownloads": self._config.skip_unknown_size_downloads,
            "bypassExtensions": self._config.bypassed_extensions,
        }

    # What the extension may write back. `port`, `host` and `enabled` are deliberately absent:
    # they decide whether this endpoint is reachable at all, so a stray write would take the
    # channel down rather than adjust a preference.
    _WRITABLE_BOOL_KEYS = (
        "intercept_all",
        "intercept_torrent_files",
        "intercept_magnet_links",
        "skip_unknown_size_downloads",
    )
    _WRITABLE_CAMEL_ALIASES = {
        "interceptDownloads": "intercept_all",
        "interceptTorrentFiles": "intercept_torrent_files",
        "interceptMagnetLinks": "intercept_magnet_links",
        "minFileSizeKb": "min_file_size_kb",
        "skipUnknownSizeDownloads": "skip_unknown_size_downloads",
        "bypassExtensions": "bypassed_extensions",
    }

    async def _handle_set_config(self, request: web.Request) -> web.Response:
        """Apply capture settings pushed from the extension's options page.

        The options page is a second front end for the same preferences, so a change made there
        has to land here: it used to be written to `chrome.storage.local` only, and the
        background worker's 30-second sync then overwrote it with the server's value - the toggle
        appeared to work and then reverted. This makes the setting genuinely shared: whichever
        side changes it, the other converges within one sync.
        """
        try:
            body = await request.json()
        except Exception as e:
            return web.json_response(
                {"status": "error", "message": f"Invalid JSON payload: {e}"},
                status=400,
                headers=self._cors_headers(),
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"status": "error", "message": "Expected a JSON object."},
                status=400,
                headers=self._cors_headers(),
            )

        merged = self._config.to_dict()
        applied: list[str] = []
        for key, value in body.items():
            key = self._WRITABLE_CAMEL_ALIASES.get(key, key)
            if key in self._WRITABLE_BOOL_KEYS:
                merged[key] = bool(value)
            elif key == "min_file_size_kb":
                try:
                    merged[key] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
            elif key == "bypassed_extensions":
                if isinstance(value, list):
                    exts = [str(v).strip() for v in value if str(v).strip()]
                    merged[key] = [e if e.startswith(".") else f".{e}" for e in exts]
                continue
            else:
                # `enabled`, `port`, `host`, and anything unknown. Not writable on purpose.
                continue
            applied.append(key)

        if not applied:
            return web.json_response(
                {
                    "status": "error",
                    "message": (
                        "No writable settings in the payload. Accepted: "
                        f"{', '.join(self._WRITABLE_BOOL_KEYS)}, min_file_size_kb, "
                        "bypassed_extensions."
                    ),
                },
                status=400,
                headers=self._cors_headers(),
            )

        updated = BrowserIntegrationConfig.from_dict(merged)
        # Apply locally first: the response is built from this server's own config, and a manager
        # is not obliged to echo the object back. `set_browser_config` then persists it to
        # QSettings and emits `browser_config_changed`, which the main window uses to re-derive
        # its capture indicators - an already-open Preferences dialog keeps its own snapshot
        # until it is reopened. Only a change to `port` or `enabled` can make the manager restart
        # the server, and neither is writable here.
        self.set_config(updated)
        setter = getattr(self._manager, "set_browser_config", None)
        if callable(setter):
            setter(updated)
        else:
            updated.save()
        log.info("Browser capture settings updated from the extension: %s", applied)

        return web.json_response(self._config_payload(), headers=self._cors_headers())

    async def _probe_content_length(
        self, url: str, headers: Optional[dict] = None, cookies: str = ""
    ) -> Optional[int]:
        """Perform a fast HEAD or range probe to determine file size in bytes.

        Thin wrapper over :func:`my_idm.http_probe.probe_url`, which the clipboard monitor uses
        for the same question and which must answer identically — a size probe that gave the two
        capture paths different answers would be indistinguishable from a bug in one of them.
        The ``Content-Type`` policy stays here: an origin serving ``text/html`` for
        ``/api/export`` is answering a browser, not offering a file, and its Content-Length is
        the size of a page rather than the size of a download.
        """
        req_headers: dict = {}
        if headers and isinstance(headers, dict):
            req_headers.update(headers)
        if cookies:
            req_headers["Cookie"] = cookies
        result = await probe_url(
            url, req_headers, timeout=1.8, connect_timeout=1.0,
            accept_html=html_is_a_file(url),
        )
        if result.ok and result.size > 0:
            return result.size
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
        extra_headers = body.get("headers", {})
        referrer = str(
            body.get("referrer")
            or body.get("referer")
            or body.get("Referer")
            or (extra_headers.get("Referer") if isinstance(extra_headers, dict) else "")
            or (extra_headers.get("referer") if isinstance(extra_headers, dict) else "")
            or ""
        ).strip()
        user_agent = body.get("user_agent", "").strip()

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
                # The size is still unknown, so the threshold cannot be applied here. Two very
                # different things can follow, and which one is right is a user preference
                # (`skip_unknown_size_downloads`).
                if self._config.skip_unknown_size_downloads:
                    # Answer `ignored` rather than queueing: the extension falls back to a
                    # native browser download, which is the only outcome that honours a minimum
                    # the user configured. Capturing first and refusing later - in the engine,
                    # after its own probe - means cancelling the browser download, adding a row,
                    # and then taking it away again, all for a file that was never wanted.
                    log.info(
                        "Not capturing browser download '%s': a minimum of %d KB is configured "
                        "and the size could not be determined (no size from the extension, no "
                        "Content-Length from the probe)",
                        filename or url, self._config.min_file_size_kb,
                    )
                    return web.json_response(
                        {
                            "status": "ignored",
                            "reason": "unknown_size",
                            "message": (
                                f"My-IDM cannot tell how large '{filename or url[:80]}' is, and a "
                                f"minimum capture size of {self._config.min_file_size_kb} KB is "
                                "configured, so the browser should download it natively. Turn off "
                                "'Skip downloads of unknown size' in Preferences to capture these "
                                "and let My-IDM check the size once it has probed the response."
                            ),
                        },
                        status=200,
                        headers=self._cors_headers(),
                    )
                # The user asked for these to be captured anyway: hand the threshold to the engine
                # instead. It probes every download before transferring a byte, so it has the
                # authoritative size and can refuse with a real reason. The `total_bytes > 0`
                # guard above means an unsizeable URL used to walk straight past a configured
                # minimum, which is how a sub-threshold file kept getting captured.
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
            self._app.router.add_post("/config", self._handle_set_config)
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
