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
        assert cfg.intercept_torrent_files is True
        assert ".crx" in cfg.bypassed_extensions
        assert ".torrent" not in cfg.bypassed_extensions

    def test_to_from_dict(self):
        cfg = BrowserIntegrationConfig(
            enabled=False,
            port=20000,
            host="localhost",
            intercept_all=False,
            intercept_torrent_files=False,
            bypassed_extensions=[".pdf", ".zip"],
        )
        d = cfg.to_dict()
        assert d["enabled"] is False
        assert d["port"] == 20000
        assert d["intercept_torrent_files"] is False
        assert d["bypassed_extensions"] == [".pdf", ".zip"]

        restored = BrowserIntegrationConfig.from_dict(d)
        assert restored.enabled is False
        assert restored.port == 20000
        assert restored.host == "localhost"
        assert restored.intercept_all is False
        assert restored.intercept_torrent_files is False
        assert restored.bypassed_extensions == [".pdf", ".zip"]

    def test_save_and_load_isolated_settings(self, tmp_path):
        ini_file = str(tmp_path / "test_settings.ini")
        settings = QSettings(ini_file, QSettings.Format.IniFormat)

        cfg = BrowserIntegrationConfig(
            enabled=True,
            port=18999,
            host="127.0.0.1",
            intercept_all=True,
            intercept_torrent_files=True,
            bypassed_extensions=[".bin", ".iso"],
        )
        cfg.save(settings)

        loaded = BrowserIntegrationConfig.load(settings)
        assert loaded.enabled is True
        assert loaded.port == 18999
        assert loaded.intercept_torrent_files is True
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
                        assert "min_file_size_kb" in cfg_data
                        assert "minFileSizeKb" in cfg_data
                        assert "interceptDownloads" in cfg_data

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

                    # 4b. POST /add with file size below min_file_size_kb threshold -> ignored
                    server.set_config(BrowserIntegrationConfig(enabled=True, port=test_port, min_file_size_kb=500))
                    small_payload = {
                        "url": "https://example.com/tiny.png",
                        "filename": "tiny.png",
                        "total_bytes": 10240,  # 10 KB < 500 KB
                    }
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json=small_payload) as resp:
                        assert resp.status == 200
                        data = await resp.json()
                        assert data["status"] == "ignored"
                        assert data["reason"] == "file_size_below_minimum"

                    # 4c. POST /add with unknown total_bytes but probe returns size below threshold -> ignored
                    async def _mock_probe(*args, **kwargs):
                        return 46 * 1024  # 46 KB < 500 KB

                    server._probe_content_length = _mock_probe
                    probe_payload = {
                        "url": "https://example.com/stream_46kb.bin",
                        "filename": "stream_46kb.bin",
                    }
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json=probe_payload) as resp:
                        assert resp.status == 200
                        data = await resp.json()
                        assert data["status"] == "ignored"
                        assert data["reason"] == "file_size_below_minimum"

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

    def test_browser_extension_urls_copyable(self, monkeypatch):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtCore import Qt

        monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)

        cfg = BrowserIntegrationConfig(enabled=True)
        dialog = SettingsDialog(browser_config=cfg, initial_tab=6)

        # 1. Verify instructions label text interaction flags
        flags = dialog._instr_lbl.textInteractionFlags()
        assert bool(flags & Qt.TextInteractionFlag.TextSelectableByMouse)
        assert bool(flags & Qt.TextInteractionFlag.LinksAccessibleByMouse)

        # 2. Test copy Chrome URL button
        dialog._on_copy_chrome_url()
        assert QApplication.clipboard().text() == "chrome://extensions/"

        # 3. Test copy Edge URL button
        dialog._on_copy_edge_url()
        assert QApplication.clipboard().text() == "edge://extensions/"

        # 4. Test copy Firefox URL button
        dialog._on_copy_firefox_url()
        assert QApplication.clipboard().text() == "about:debugging#/runtime/this-firefox"

        # 4b. Test copy Firefox Add-ons URL button
        dialog._on_copy_firefox_addons_url()
        assert QApplication.clipboard().text() == "about:addons"

        # 5. Test link click handler
        dialog._on_browser_url_clicked("chrome://extensions/")
        assert QApplication.clipboard().text() == "chrome://extensions/"

        dialog._on_browser_url_clicked("copy:extension_path")
        assert "browser_extension" in QApplication.clipboard().text()

        dialog._on_browser_url_clicked("copy:manifest_path")
        assert "manifest.json" in QApplication.clipboard().text()

        # 6. Test Firefox packaging button
        dialog._on_package_firefox_extension()

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

        # Firefox compatibility checks in main manifest.json
        assert "service_worker" in manifest["background"]
        assert "scripts" in manifest["background"]
        assert "background.js" in manifest["background"]["scripts"]
        assert "browser_specific_settings" in manifest
        assert "gecko" in manifest["browser_specific_settings"]
        assert manifest["browser_specific_settings"]["gecko"].get("id") == "my-idm@local"
        assert manifest["browser_specific_settings"]["gecko"].get("strict_min_version") == "109.0"

        # Content script checks
        assert "content_scripts" in manifest
        assert any("content.js" in cs.get("js", []) for cs in manifest["content_scripts"])

        assert (ext_dir / "background.js").is_file()
        assert (ext_dir / "content.js").is_file()
        assert (ext_dir / "popup.html").is_file()
        assert (ext_dir / "popup.js").is_file()
        assert (ext_dir / "options.html").is_file()
        assert (ext_dir / "options.js").is_file()
        assert (ext_dir / "styles.css").is_file()

        # Dedicated Firefox manifest check
        firefox_manifest_path = ext_dir / "manifest.firefox.json"
        assert firefox_manifest_path.is_file()
        with open(firefox_manifest_path, "r", encoding="utf-8") as f:
            ff_manifest = json.load(f)
        assert ff_manifest.get("manifest_version") == 3
        assert "scripts" in ff_manifest.get("background", {})
        assert ff_manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id") == "my-idm@local"

        icons_dir = ext_dir / "icons"
        assert (icons_dir / "icon16.png").is_file()
        assert (icons_dir / "icon48.png").is_file()
        assert (icons_dir / "icon128.png").is_file()


class TestFirefoxExtensionPackaging:
    """Verify Firefox .xpi packaging logic and installation guide dialog."""

    def test_package_firefox_extension(self, tmp_path):
        import zipfile
        from my_idm.extension_packager import package_firefox_extension, get_default_extension_dir

        out_xpi = tmp_path / "test_addon.xpi"
        result_path = package_firefox_extension(output_path=out_xpi)

        assert result_path == out_xpi
        assert out_xpi.is_file()
        assert out_xpi.stat().st_size > 0

        with zipfile.ZipFile(out_xpi, "r") as zf:
            namelist = zf.namelist()
            assert "manifest.json" in namelist
            assert "manifest.firefox.json" not in namelist
            assert "background.js" in namelist
            assert "content.js" in namelist
            assert "popup.html" in namelist
            assert "popup.js" in namelist
            assert "options.html" in namelist
            assert "options.js" in namelist
            assert "styles.css" in namelist
            assert "icons/icon16.png" in namelist
            assert "icons/icon48.png" in namelist
            assert "icons/icon128.png" in namelist

            # Verify manifest content is tailored for Firefox MV3
            with zf.open("manifest.json") as mf:
                manifest_data = json.load(mf)
                assert manifest_data.get("manifest_version") == 3
                assert "scripts" in manifest_data.get("background", {})
                assert manifest_data.get("browser_specific_settings", {}).get("gecko", {}).get("id") == "my-idm@local"

    def test_firefox_install_guide_dialog(self, monkeypatch):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtGui import QDesktopServices
        from my_idm.settings_dialog import FirefoxInstallGuideDialog

        monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
        opened_urls = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened_urls.append(url.toString()))

        dialog = FirefoxInstallGuideDialog()

        # Test copying about:config and about:addons
        dialog._copy_text("about:config", "test")
        QApplication.processEvents()
        assert QApplication.clipboard().text() == "about:config"

        dialog._copy_text("about:addons", "test")
        QApplication.processEvents()
        assert QApplication.clipboard().text() == "about:addons"

        # Test link click handler for non-http
        dialog._on_link_clicked("about:config")
        QApplication.processEvents()
        assert QApplication.clipboard().text() == "about:config"

        dialog._on_link_clicked("xpinstall.signatures.required")
        QApplication.processEvents()
        assert QApplication.clipboard().text() == "xpinstall.signatures.required"

        dialog._on_link_clicked("false")
        QApplication.processEvents()
        assert QApplication.clipboard().text() == "false"

        dialog._on_link_clicked("copy:xpi_path")
        QApplication.processEvents()
        assert "my-idm-firefox.xpi" in QApplication.clipboard().text()

        # Test external link routing to QDesktopServices
        dialog._on_link_clicked("https://addons.mozilla.org/developers/addon/submit/distribution")
        assert len(opened_urls) == 1
        assert "addons.mozilla.org" in opened_urls[0]

        # Test packaging triggered from guide
        dialog._on_package_clicked()

        dialog.close()


class TestEdgeBrowserIntegration:
    """Test Microsoft Edge specific browser integration workflows and compatibility."""

    def test_edge_download_metadata_and_headers(self, tmp_path):
        db_path = tmp_path / "test_edge.db"
        db = Database(str(db_path))
        db.open()

        manager = DownloadManager(db)

        edge_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
        )
        did = manager.add_download_from_browser(
            url="https://edge.microsoft.com/test_setup.exe",
            filename="test_setup.exe",
            save_path=str(tmp_path),
            headers={"Sec-CH-UA": '"Microsoft Edge";v="128"'},
            cookies="edge_session=edg123;",
            referrer="https://www.microsoft.com/edge",
            user_agent=edge_ua,
        )

        assert did is not None
        entry = db.get_download(did)
        assert entry is not None
        assert entry.metadata.get("source") == "browser_extension"
        assert entry.metadata.get("user_agent") == edge_ua
        assert "Edg/128.0.0.0" in entry.metadata["headers"]["User-Agent"]
        assert entry.metadata["headers"]["Sec-CH-UA"] == '"Microsoft Edge";v="128"'
        db.close()

    def test_edge_url_copying_and_links(self):
        cfg = BrowserIntegrationConfig(enabled=True)
        dialog = SettingsDialog(browser_config=cfg, initial_tab=6)

        # 1. Direct Edge copy method
        dialog._on_copy_edge_url()
        assert QApplication.clipboard().text() == "edge://extensions/"

        # 2. Clicking edge://extensions/ URL link
        dialog._on_browser_url_clicked("edge://extensions/")
        assert QApplication.clipboard().text() == "edge://extensions/"

        # 3. Label text and group title contain Edge references
        raw_html = dialog._chrome_instr_lbl.text()
        assert "edge://extensions/" in raw_html
        assert "Edge" in dialog._chrome_group.title()

        dialog.close()


class TestBrowserServerSizeFilterAndProbe:
    """Test probe_content_length and size filtering logic directly."""

    def test_probe_content_length_head_success(self):
        mock_manager = MagicMock()
        cfg = BrowserIntegrationConfig(enabled=True, min_file_size_kb=500)
        server = BrowserServer(mock_manager, cfg)

        class MockHeadResponse:
            status = 200
            headers = {"Content-Length": "10485760", "Content-Type": "application/octet-stream"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class MockSession:
            def head(self, *args, **kwargs):
                return MockHeadResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        async def _test():
            import unittest.mock as mock
            with mock.patch("aiohttp.ClientSession", return_value=MockSession()):
                length = await server._probe_content_length("https://example.com/bigfile.bin")
                assert length == 10485760

        asyncio.run(_test())

    def test_probe_content_length_get_range_fallback(self):
        mock_manager = MagicMock()
        cfg = BrowserIntegrationConfig(enabled=True, min_file_size_kb=500)
        server = BrowserServer(mock_manager, cfg)

        class MockHeadFailed:
            status = 405
            headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class MockGetRangeSuccess:
            status = 206
            headers = {
                "Content-Range": "bytes 0-0/5242880",
                "Content-Type": "application/zip",
            }

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class MockSession:
            def head(self, *args, **kwargs):
                return MockHeadFailed()

            def get(self, *args, **kwargs):
                return MockGetRangeSuccess()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        async def _test():
            import unittest.mock as mock
            with mock.patch("aiohttp.ClientSession", return_value=MockSession()):
                length = await server._probe_content_length("https://example.com/ranged_file.zip")
                assert length == 5242880

        asyncio.run(_test())

    def test_probe_content_length_html_ignored(self):
        mock_manager = MagicMock()
        cfg = BrowserIntegrationConfig(enabled=True, min_file_size_kb=500)
        server = BrowserServer(mock_manager, cfg)

        class MockHtmlHead:
            status = 200
            headers = {"Content-Length": "1000", "Content-Type": "text/html; charset=utf-8"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class MockHtmlGet:
            status = 200
            headers = {"Content-Length": "1000", "Content-Type": "text/html; charset=utf-8"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class MockSession:
            def head(self, *args, **kwargs):
                return MockHtmlHead()

            def get(self, *args, **kwargs):
                return MockHtmlGet()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        async def _test():
            import unittest.mock as mock
            with mock.patch("aiohttp.ClientSession", return_value=MockSession()):
                length = await server._probe_content_length("https://example.com/download_page")
                assert length is None

        asyncio.run(_test())

    def test_magnet_link_bypasses_size_threshold(self):
        mock_manager = MagicMock()
        mock_manager.add_download_from_browser.return_value = "mag-999"

        test_port = 19602

        async def _run():
            # Configure 10,000 KB minimum size
            cfg = BrowserIntegrationConfig(enabled=True, port=test_port, min_file_size_kb=10000)
            server = BrowserServer(mock_manager, cfg)

            started = await server.start()
            assert started is True

            try:
                async with aiohttp.ClientSession() as session:
                    magnet_payload = {
                        "url": "magnet:?xt=urn:btih:d1234567890abcdef1234567890abcdef1234567&dn=TestTorrent",
                        "filename": "TestTorrent",
                    }
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json=magnet_payload) as resp:
                        assert resp.status == 200
                        data = await resp.json()
                        assert data["status"] == "ok"
                        assert data["id"] == "mag-999"

                    mock_manager.add_download_from_browser.assert_called_once()
            finally:
                await server.stop()

        asyncio.run(_run())

