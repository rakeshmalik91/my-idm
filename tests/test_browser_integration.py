"""Unit tests for Chrome browser extension integration and loopback REST server."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock
import aiohttp
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from my_idm.config import BrowserIntegrationConfig
from my_idm.browser_server import BrowserServer
from my_idm.database import Database
from my_idm.manager import DownloadManager
from my_idm.settings_dialog import SettingsDialog

app = QApplication.instance() or QApplication(sys.argv)


class TestBrowserIntegrationConfig:
    """Test BrowserIntegrationConfig defaults, serialization, and settings persistence."""

    def test_default_config(self):
        cfg = BrowserIntegrationConfig()
        assert cfg.enabled is True
        assert cfg.port == 19582
        assert cfg.host == "127.0.0.1"
        assert cfg.intercept_all is True
        assert ".torrent" in cfg.bypassed_extensions
        assert ".crx" in cfg.bypassed_extensions

    def test_to_from_dict(self):
        cfg = BrowserIntegrationConfig(
            enabled=False,
            port=20000,
            host="localhost",
            intercept_all=False,
            bypassed_extensions=[".pdf", ".zip"],
        )
        d = cfg.to_dict()
        assert d["enabled"] is False
        assert d["port"] == 20000
        assert d["bypassed_extensions"] == [".pdf", ".zip"]

        restored = BrowserIntegrationConfig.from_dict(d)
        assert restored.enabled is False
        assert restored.port == 20000
        assert restored.host == "localhost"
        assert restored.intercept_all is False
        assert restored.bypassed_extensions == [".pdf", ".zip"]

    def test_save_and_load_isolated_settings(self, tmp_path):
        ini_file = str(tmp_path / "test_settings.ini")
        settings = QSettings(ini_file, QSettings.Format.IniFormat)

        cfg = BrowserIntegrationConfig(
            enabled=True,
            port=18999,
            host="127.0.0.1",
            intercept_all=True,
            bypassed_extensions=[".bin", ".iso"],
        )
        cfg.save(settings)

        loaded = BrowserIntegrationConfig.load(settings)
        assert loaded.enabled is True
        assert loaded.port == 18999
        assert loaded.bypassed_extensions == [".bin", ".iso"]


class TestBrowserServerLiveEndpoints:
    """Test REST API routes on live BrowserServer using actual aiohttp ClientSession."""

    def test_server_lifecycle_and_endpoints(self):
        mock_manager = MagicMock()
        mock_manager._get_active_download_count.return_value = 2
        mock_manager.add_download_from_browser.return_value = "dl-12345"

        test_port = 19598

        async def _run():
            cfg = BrowserIntegrationConfig(enabled=True, port=test_port)
            server = BrowserServer(mock_manager, cfg)

            started = await server.start()
            assert started is True
            assert server.is_running is True

            try:
                async with aiohttp.ClientSession() as session:
                    # 1. OPTIONS CORS preflight check
                    async with session.options(f"http://127.0.0.1:{test_port}/health") as resp:
                        assert resp.status == 200
                        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

                    # 2. GET /health
                    async with session.get(f"http://127.0.0.1:{test_port}/health") as resp:
                        assert resp.status == 200
                        data = await resp.json()
                        assert data["status"] == "ok"
                        assert data["app"] == "My-IDM"
                        assert data["active_downloads"] == 2

                    # 3. GET /config
                    async with session.get(f"http://127.0.0.1:{test_port}/config") as resp:
                        assert resp.status == 200
                        cfg_data = await resp.json()
                        assert cfg_data["status"] == "ok"
                        assert cfg_data["enabled"] is True
                        assert cfg_data["port"] == test_port

                    # 4. POST /add with full payload
                    payload = {
                        "url": "https://example.com/testfile.iso",
                        "filename": "custom_name.iso",
                        "save_path": "D:/Downloads",
                        "cookies": "session=abc123xyz; user=john",
                        "referrer": "https://example.com/downloads",
                        "user_agent": "Mozilla/5.0 TestBrowser",
                        "headers": {"X-Custom": "val123"}
                    }
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json=payload) as resp:
                        assert resp.status == 200
                        data = await resp.json()
                        assert data["status"] == "ok"
                        assert data["id"] == "dl-12345"

                    mock_manager.add_download_from_browser.assert_called_once_with(
                        url="https://example.com/testfile.iso",
                        filename="custom_name.iso",
                        save_path="D:/Downloads",
                        headers={"X-Custom": "val123"},
                        cookies="session=abc123xyz; user=john",
                        referrer="https://example.com/downloads",
                        user_agent="Mozilla/5.0 TestBrowser",
                    )

                    # 5. POST /add missing url -> 400
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json={"filename": "only.zip"}) as resp:
                        assert resp.status == 400
                        data = await resp.json()
                        assert data["status"] == "error"

                    # 6. Integration disabled -> 403
                    server.set_config(BrowserIntegrationConfig(enabled=False, port=test_port))
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json={"url": "https://example.com/test.zip"}) as resp:
                        assert resp.status == 403
                        data = await resp.json()
                        assert data["status"] == "error"

            finally:
                await server.stop()
                assert server.is_running is False

        asyncio.run(_run())


class TestManagerBrowserIntegration:
    """Test manager methods for adding browser downloads and headers formatting."""

    def test_add_download_from_browser(self, tmp_path):
        db_path = tmp_path / "test_browser.db"
        db = Database(str(db_path))
        db.open()

        manager = DownloadManager(db)

        did = manager.add_download_from_browser(
            url="https://example.com/browser_file.zip",
            filename="my_browser_file.zip",
            save_path=str(tmp_path),
            headers={"Authorization": "Bearer token123"},
            cookies={"sid": "session999", "theme": "dark"},
            referrer="https://example.com/home",
            user_agent="MyTestUserAgent/1.0",
        )

        assert did is not None
        entry = db.get_download(did)
        assert entry is not None
        assert entry.url == "https://example.com/browser_file.zip"
        assert entry.filename == "my_browser_file.zip"
        assert entry.metadata.get("source") == "browser_extension"
        assert entry.metadata.get("referer") == "https://example.com/home"
        assert entry.metadata.get("user_agent") == "MyTestUserAgent/1.0"
        assert "headers" in entry.metadata
        req_headers = entry.metadata["headers"]
        assert req_headers.get("Authorization") == "Bearer token123"
        assert req_headers.get("Referer") == "https://example.com/home"
        assert req_headers.get("User-Agent") == "MyTestUserAgent/1.0"
        assert "sid=session999" in req_headers.get("Cookie", "")

        db.close()

    def test_manager_browser_config_update(self, tmp_path):
        db_path = tmp_path / "test_browser2.db"
        db = Database(str(db_path))
        db.open()

        manager = DownloadManager(db)
        signal_received = []
        manager.browser_config_changed.connect(lambda cfg: signal_received.append(cfg))

        new_cfg = BrowserIntegrationConfig(enabled=False, port=20123)
        manager.set_browser_config(new_cfg)

        assert manager.browser_config.enabled is False
        assert manager.browser_config.port == 20123
        assert len(signal_received) == 1
        assert signal_received[0].port == 20123

        db.close()


class TestSettingsDialogBrowserTab:
    """Test SettingsDialog rendering and saving of browser integration tab."""

    def test_settings_dialog_browser_tab(self, tmp_path):
        cfg = BrowserIntegrationConfig(
            enabled=True,
            port=19582,
            intercept_all=True,
            bypassed_extensions=[".torrent", ".crx"],
        )
        dialog = SettingsDialog(browser_config=cfg, initial_tab=6)

        assert dialog._browser_enabled_cb.isChecked() is True
        assert dialog._browser_port_spin.value() == 19582
        assert dialog._browser_intercept_cb.isChecked() is True
        assert ".torrent" in dialog._browser_bypass_edit.text()

        # Modify values
        dialog._browser_port_spin.setValue(19800)
        dialog._browser_intercept_cb.setChecked(False)
        dialog._browser_bypass_edit.setText(".torrent, .crx, .iso")

        # Save and verify
        dialog._on_save()
        saved_cfg = dialog.browser_config
        assert saved_cfg.port == 19800
        assert saved_cfg.intercept_all is False
        assert ".iso" in saved_cfg.bypassed_extensions
        dialog.close()


class TestBrowserExtensionFiles:
    """Verify presence and validity of extension files in browser_extension/."""

    def test_extension_folder_structure(self):
        ext_dir = Path(__file__).resolve().parent.parent / "browser_extension"
        assert ext_dir.is_dir()

        manifest_path = ext_dir / "manifest.json"
        assert manifest_path.is_file()

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        assert manifest.get("manifest_version") == 3
        assert "downloads" in manifest.get("permissions", [])
        assert "cookies" in manifest.get("permissions", [])
        assert "background" in manifest

        assert (ext_dir / "background.js").is_file()
        assert (ext_dir / "popup.html").is_file()
        assert (ext_dir / "popup.js").is_file()
        assert (ext_dir / "options.html").is_file()
        assert (ext_dir / "options.js").is_file()
        assert (ext_dir / "styles.css").is_file()

        icons_dir = ext_dir / "icons"
        assert (icons_dir / "icon16.png").is_file()
        assert (icons_dir / "icon48.png").is_file()
        assert (icons_dir / "icon128.png").is_file()
