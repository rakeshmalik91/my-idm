"""Embedded loopback REST server for Chrome extension integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING, Union
from aiohttp import web

from my_idm.config import BrowserIntegrationConfig

if TYPE_CHECKING:
    from my_idm.manager import DownloadManager

log = logging.getLogger(__name__)


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
            "intercept_all": self._config.intercept_all,
            "bypassed_extensions": self._config.bypassed_extensions,
        }
        return web.json_response(data, headers=self._cors_headers())

    async def _handle_add(self, request: web.Request) -> web.Response:
        if not self._config.enabled:
            return web.json_response(
                {"status": "error", "message": "Browser integration is disabled in My-IDM settings."},
                status=403,
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

        filename = body.get("filename", "").strip()
        save_path = body.get("save_path", "").strip()
        cookies = body.get("cookies", "")
        referrer = body.get("referrer", "").strip()
        user_agent = body.get("user_agent", "").strip()
        extra_headers = body.get("headers", {})

        try:
            download_id = self._manager.add_download_from_browser(
                url=url,
                filename=filename,
                save_path=save_path,
                headers=extra_headers if isinstance(extra_headers, dict) else {},
                cookies=cookies,
                referrer=referrer,
                user_agent=user_agent,
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
