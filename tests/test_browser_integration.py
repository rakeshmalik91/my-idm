"""Unit tests for Chrome browser extension integration and loopback REST server."""

import asyncio
import json
import socket
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import aiohttp
import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

import my_idm.extension_packager as extension_packager
from my_idm.browser_server import BrowserServer
from my_idm.config import BrowserIntegrationConfig
from my_idm.database import Database
from my_idm.manager import DownloadManager
from my_idm.settings_dialog import FirefoxInstallGuideDialog, SettingsDialog

app = QApplication.instance() or QApplication(sys.argv)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = REPO_ROOT / "browser_extension"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def free_tcp_port() -> int:
    """Return a currently-free loopback TCP port.

    The browser integration server really binds a ``web.TCPSite``, so a hardcoded
    port collides with a running My-IDM (or a parallel pytest worker) and turns a
    green suite red for reasons that have nothing to do with the code under test.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def assert_port_released(port: int, context: str) -> None:
    """Fail unless ``port`` can be bound again, i.e. the server really let it go."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            pytest.fail(
                f"TCP port {port} was not released after {context}: {exc}. "
                "A leaked BrowserServer keeps the listening socket (and the port) "
                "for the rest of the session."
            )


class _FakeClipboard:
    """In-memory ``QClipboard`` stand-in.

    The Windows clipboard is an asynchronous, process-global resource: a transient
    lock by another process makes ``QClipboard.setText`` a silent no-op, so any
    assertion that reads it back is a coin flip (and any test that writes to it
    destroys the developer's clipboard for the session). Production code reaches
    the clipboard only through ``QApplication.clipboard()``, so replacing that one
    accessor makes these tests deterministic and order-independent while still
    asserting exactly what the code tried to copy.
    """

    def __init__(self):
        self._text = ""
        self._image = None
        self.set_calls: list[str] = []
        self.clear_calls = 0

    def text(self, mode=None):
        return self._text

    def setText(self, text, mode=None):
        self._text = text or ""
        self.set_calls.append(self._text)

    def clear(self, mode=None):
        self.clear_calls += 1
        self._text = ""
        self._image = None

    def image(self, mode=None):
        return self._image

    def setImage(self, image, mode=None):
        self._image = image

    def setPixmap(self, pixmap, mode=None):
        self._image = pixmap

    def pixmap(self, mode=None):
        return self._image

    def mimeData(self, formats=None):
        return None

    def supportsSelection(self):
        return False

    def ownsClipboard(self):
        return False

    def ownsSelection(self):
        return False


def install_fake_clipboard(monkeypatch):
    """Point ``QApplication.clipboard()`` at a fresh in-memory clipboard."""
    fake = _FakeClipboard()
    monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: fake))
    return fake


def assert_clipboard(fake, expected: str, context: str) -> None:
    """Assert the clipboard holds exactly ``expected``.

    Deliberately *not* a "return the first non-empty value" helper: a stale value
    from an earlier copy in the same test would satisfy that silently. The setText
    log is included so a failure says what the code actually tried to copy.
    """
    actual = fake.text()
    assert actual == expected, (
        f"clipboard mismatch for {context}: expected {expected!r}, got {actual!r}; "
        f"setText call log: {fake.set_calls!r}"
    )


def copy_and_assert_clipboard(action, expected: str, context: str, fake) -> None:
    """Run a copy handler, then assert the clipboard holds exactly ``expected``."""
    action()
    assert_clipboard(fake, expected, context)


def patch_packager_into(tmp_path, monkeypatch):
    """Redirect ``package_firefox_extension`` into ``tmp_path`` and record calls.

    ``SettingsDialog._on_package_firefox_extension`` and
    ``FirefoxInstallGuideDialog._on_package_clicked`` both import the packager
    *inside* the function body, so the only correct patch target is the source
    module attribute. Without this, the real packager runs with
    ``output_path=None`` and writes ``browser_extension/my-idm-firefox.xpi``
    into the repository source tree.

    The real packager still runs (into tmp_path) so the produced archive is a
    genuine .xpi rather than a stub.
    """
    real_packager = extension_packager.package_firefox_extension
    produced: dict[str, Path] = {}

    def _redirect(*args, **kwargs):
        assert not args and not kwargs, (
            "production code must not pass an explicit output_path; the test "
            f"redirects the archive into tmp_path instead (got args={args!r} "
            f"kwargs={kwargs!r})"
        )
        target = tmp_path / "packaged" / "my-idm-firefox.xpi"
        produced["path"] = target
        return real_packager(output_path=target)

    spy = mock.Mock(side_effect=_redirect, name="package_firefox_extension")
    monkeypatch.setattr(extension_packager, "package_firefox_extension", spy)
    return spy, produced


def record_folder_reveals(monkeypatch):
    """Patch ``show_in_folder`` so no test can spawn a real Explorer window."""
    revealed: list[Path] = []

    def _fake_show(target):
        revealed.append(target)
        return True, ""

    monkeypatch.setattr("my_idm.settings_dialog.show_in_folder", _fake_show)
    return revealed


def record_message_boxes(monkeypatch, information_answer, allow_critical=False):
    """Patch QMessageBox.information/critical and record what was shown.

    By default an unexpected *failure* dialog fails the test instead of being
    silently recorded, which is what caught the packaging paths.
    """
    information_calls: list[tuple] = []
    critical_calls: list[tuple] = []

    def _information(*args, **kwargs):
        information_calls.append(args)
        return information_answer

    def _critical(*args, **kwargs):
        if not allow_critical:
            pytest.fail(
                f"a failure dialog was raised during a path expected to succeed: {args!r}"
            )
        critical_calls.append(args)
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "information", _information)
    monkeypatch.setattr(QMessageBox, "critical", _critical)
    return information_calls, critical_calls


def _no_stray_repo_xpi():
    """Snapshot the repo-local .xpi so a test can prove it did not rewrite it."""
    stray = EXTENSION_DIR / "my-idm-firefox.xpi"
    stat = stray.stat() if stray.is_file() else None
    return stray, (stat.st_mtime_ns, stat.st_size) if stat else None


def _assert_repo_xpi_untouched(before):
    stray, fingerprint = before
    now_stat = stray.stat() if stray.is_file() else None
    now = (now_stat.st_mtime_ns, now_stat.st_size) if now_stat else None
    assert now == fingerprint, (
        f"{stray} was created or rewritten by the test run. Packaging must be "
        "redirected into tmp_path so the repository source tree stays clean."
    )


class TestBrowserIntegrationConfig:
    """Test BrowserIntegrationConfig defaults, serialization, and settings persistence."""

    def test_default_config(self):
        cfg = BrowserIntegrationConfig()
        assert cfg.enabled is True, "browser integration must be on by default"
        assert cfg.port == 19582, f"default loopback port changed: {cfg.port}"
        assert cfg.host == "127.0.0.1", "server must never bind a non-loopback interface"
        assert cfg.intercept_all is True
        assert cfg.intercept_torrent_files is True
        assert ".crx" in cfg.bypassed_extensions, "browser extension bundles must never be intercepted"
        assert ".torrent" not in cfg.bypassed_extensions, "torrent files must stay interceptable"

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
        assert restored.bypassed_extensions == [".pdf", ".zip"], (
            "from_dict must round-trip the bypass list verbatim, "
            f"got {restored.bypassed_extensions!r}"
        )

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
        assert loaded.port == 18999, f"persisted port was not reloaded: {loaded.port}"
        assert loaded.intercept_torrent_files is True
        assert loaded.bypassed_extensions == [".bin", ".iso"]


class TestBrowserServerLiveEndpoints:
    """Test REST API routes on live BrowserServer using actual aiohttp ClientSession."""

    def test_server_lifecycle_and_endpoints(self):
        mock_manager = MagicMock()
        mock_manager._get_active_download_count.return_value = 2
        mock_manager.add_download_from_browser.return_value = "dl-12345"

        test_port = free_tcp_port()

        async def _run():
            cfg = BrowserIntegrationConfig(enabled=True, port=test_port)
            server = BrowserServer(mock_manager, cfg)

            started = await server.start()
            assert started is True, (
                f"BrowserServer failed to bind 127.0.0.1:{test_port}; a hardcoded "
                "port collides with a running app or a parallel worker"
            )
            assert server.is_running is True

            try:
                async with aiohttp.ClientSession() as session:
                    # 1. OPTIONS CORS preflight check
                    async with session.options(f"http://127.0.0.1:{test_port}/health") as resp:
                        assert resp.status == 200, "CORS preflight must succeed for the extension"
                        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

                    # 2. GET /health
                    async with session.get(f"http://127.0.0.1:{test_port}/health") as resp:
                        assert resp.status == 200
                        data = await resp.json()
                        assert data["status"] == "ok"
                        assert data["app"] == "My-IDM"
                        assert data["active_downloads"] == 2, (
                            f"/health must report the live active count, got "
                            f"{data['active_downloads']!r}"
                        )

                    # 3. GET /config
                    async with session.get(f"http://127.0.0.1:{test_port}/config") as resp:
                        assert resp.status == 200
                        cfg_data = await resp.json()
                        assert cfg_data["status"] == "ok"
                        assert cfg_data["enabled"] is True
                        assert cfg_data["port"] == test_port, (
                            "/config must echo the port the server actually bound, "
                            f"expected {test_port}, got {cfg_data['port']!r}"
                        )
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
                        assert data["id"] == "dl-12345", "/add must return the manager's download id"

                    mock_manager.add_download_from_browser.assert_called_once_with(
                        url="https://example.com/testfile.iso",
                        filename="custom_name.iso",
                        save_path="D:/Downloads",
                        headers={"X-Custom": "val123"},
                        cookies="session=abc123xyz; user=john",
                        referrer="https://example.com/downloads",
                        user_agent="Mozilla/5.0 TestBrowser",
                        # No minimum configured in this test, so nothing is deferred to the
                        # engine's own probe. See tests/test_browser_capture_gating.py.
                        pending_min_bytes=0,
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
                        assert data["status"] == "ignored", "10 KB must be below the 500 KB floor"
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
                        assert data["status"] == "ignored", (
                            "the HEAD/range probe result must drive the size filter when the "
                            "extension does not send total_bytes"
                        )
                        assert data["reason"] == "file_size_below_minimum"

                    # 5. POST /add missing url -> 400
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json={"filename": "only.zip"}) as resp:
                        assert resp.status == 400, "a payload without 'url' must be rejected with 400"
                        data = await resp.json()
                        assert data["status"] == "error"

                    # 6. Integration disabled -> 403
                    server.set_config(BrowserIntegrationConfig(enabled=False, port=test_port))
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json={"url": "https://example.com/test.zip"}) as resp:
                        assert resp.status == 403, "a disabled server must refuse new downloads"
                        data = await resp.json()
                        assert data["status"] == "error"

            finally:
                await server.stop()
                assert server.is_running is False, "stop() must clear the running flag"
                assert server._site is None, "stop() must drop the TCPSite reference"
                assert server._runner is None, "stop() must drop the AppRunner reference"

        asyncio.run(_run())
        assert_port_released(test_port, "BrowserServer.stop()")

    def test_repeated_start_stop_on_same_port_is_reusable(self):
        """A second server can bind the same port after the first one was stopped."""
        mock_manager = MagicMock()
        mock_manager._get_active_download_count.return_value = 0
        test_port = free_tcp_port()

        async def _run():
            for attempt in (1, 2):
                server = BrowserServer(
                    mock_manager,
                    BrowserIntegrationConfig(enabled=True, port=test_port),
                )
                started = await server.start()
                assert started is True, f"start #{attempt} failed on port {test_port}"
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f"http://127.0.0.1:{test_port}/health") as resp:
                            assert resp.status == 200, f"health unreachable on start #{attempt}"
                finally:
                    await server.stop()
                assert server.is_running is False

        asyncio.run(_run())
        assert_port_released(test_port, "two sequential start/stop cycles")


class TestManagerBrowserIntegration:
    """Test manager methods for adding browser downloads and headers formatting."""

    def test_add_download_from_browser(self, tmp_path):
        db_path = tmp_path / "test_browser.db"
        db = Database(str(db_path))
        db.open()

        manager = DownloadManager(db)
        try:
            did = manager.add_download_from_browser(
                url="https://example.com/browser_file.zip",
                filename="my_browser_file.zip",
                save_path=str(tmp_path),
                headers={"Authorization": "Bearer token123"},
                cookies={"sid": "session999", "theme": "dark"},
                referrer="https://example.com/home",
                user_agent="MyTestUserAgent/1.0",
            )

            assert isinstance(did, str) and did, (
                f"add_download_from_browser must return a non-empty download id, got {did!r}"
            )
            entry = db.get_download(did)
            assert entry is not None, f"no database row was persisted for id {did!r}"
            assert entry.id == did, "the persisted row must carry the returned id"
            assert entry.url == "https://example.com/browser_file.zip"
            assert entry.filename == "my_browser_file.zip"
            assert Path(entry.save_path) == tmp_path, (
                "the browser-supplied save_path must win over the configured default, "
                f"got {entry.save_path!r}"
            )
            assert entry.status == "queued", (
                f"a browser download must be queued (not started) by the manager, got {entry.status!r}"
            )
            assert entry.download_type == "http"
            assert entry.metadata.get("source") == "browser_extension"
            assert entry.metadata.get("referer") == "https://example.com/home"
            assert entry.metadata.get("user_agent") == "MyTestUserAgent/1.0"
            assert entry.metadata.get("cookies") == {"sid": "session999", "theme": "dark"}, (
                "the raw cookie mapping must be kept in metadata for the UI, got "
                f"{entry.metadata.get('cookies')!r}"
            )
            assert "headers" in entry.metadata
            req_headers = entry.metadata["headers"]
            assert req_headers.get("Authorization") == "Bearer token123", (
                "caller-supplied headers must survive verbatim"
            )
            assert req_headers.get("Referer") == "https://example.com/home"
            assert req_headers.get("User-Agent") == "MyTestUserAgent/1.0"
            cookie_header = req_headers.get("Cookie", "")
            assert "sid=session999" in cookie_header, (
                f"dict cookies must be flattened into a Cookie header, got {cookie_header!r}"
            )
            assert "theme=dark" in cookie_header
        finally:
            manager.stop()
            db.close()

    def test_manager_browser_config_update(self, tmp_path):
        db_path = tmp_path / "test_browser2.db"
        db = Database(str(db_path))
        db.open()

        manager = DownloadManager(db)
        try:
            signal_received = []
            manager.browser_config_changed.connect(lambda cfg: signal_received.append(cfg))

            new_cfg = BrowserIntegrationConfig(enabled=False, port=20123)
            manager.set_browser_config(new_cfg)

            assert manager.browser_config is new_cfg, (
                "set_browser_config must swap in the exact config object, not a copy"
            )
            assert manager.browser_config.enabled is False
            assert manager.browser_config.port == 20123
            assert manager._browser_server.config is new_cfg, (
                "the embedded BrowserServer must be reconfigured together with the manager"
            )
            assert len(signal_received) == 1, (
                f"exactly one browser_config_changed signal expected, got {len(signal_received)}"
            )
            assert signal_received[0].port == 20123
        finally:
            # set_browser_config() calls config.save(); a leaked manager keeps its
            # QTimers and engines alive for the rest of the session.
            manager.stop()
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
        dialog = SettingsDialog(browser_config=cfg, initial_tab=2)
        try:
            assert dialog._browser_enabled_cb.isChecked() is True
            assert dialog._browser_port_spin.value() == 19582
            assert dialog._browser_intercept_cb.isChecked() is True
            assert ".torrent" in dialog._browser_bypass_edit.text()

            # Modify values
            dialog._browser_port_spin.setValue(19800)
            dialog._browser_intercept_cb.setChecked(False)
            dialog._browser_bypass_edit.setText(".torrent, .crx, .iso")

            # _on_save() does `os.makedirs(DEFAULT_DOWNLOADS_DIR)` when the save-path
            # field is empty, which would create a real ~/Downloads on the host.
            # Point it at tmp_path so nothing outside the test sandbox is created.
            save_dir = tmp_path / "downloads"
            assert not save_dir.exists()
            dialog._save_path_edit.setText(str(save_dir))

            # Save and verify
            dialog._on_save()
            saved_cfg = dialog.browser_config
            assert saved_cfg.port == 19800
            assert saved_cfg.intercept_all is False
            assert ".iso" in saved_cfg.bypassed_extensions
            assert save_dir.is_dir(), (
                f"_on_save() must create the configured download directory {save_dir}"
            )
            assert dialog.general_config.default_save_path == str(save_dir), (
                "the save-path field must be persisted verbatim; got "
                f"{dialog.general_config.default_save_path!r}"
            )
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.processEvents()

    def test_browser_extension_urls_copyable(self, monkeypatch, tmp_path):
        repo_xpi_before = _no_stray_repo_xpi()
        monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
        clip = install_fake_clipboard(monkeypatch)

        cfg = BrowserIntegrationConfig(enabled=True)
        dialog = SettingsDialog(browser_config=cfg, initial_tab=2)
        try:
            # 1. Verify instructions label text interaction flags
            flags = dialog._instr_lbl.textInteractionFlags()
            assert bool(flags & Qt.TextInteractionFlag.TextSelectableByMouse), (
                "the instructions text must be mouse-selectable so users can copy it"
            )
            assert bool(flags & Qt.TextInteractionFlag.LinksAccessibleByMouse)

            # 2. Test copy Chrome URL button
            copy_and_assert_clipboard(
                dialog._on_copy_chrome_url,
                "chrome://extensions/",
                "_on_copy_chrome_url",
                clip,
            )

            # 3. Test copy Edge URL button
            copy_and_assert_clipboard(
                dialog._on_copy_edge_url,
                "edge://extensions/",
                "_on_copy_edge_url",
                clip,
            )

            # 4. Test copy Firefox URL button
            copy_and_assert_clipboard(
                dialog._on_copy_firefox_url,
                "about:debugging#/runtime/this-firefox",
                "_on_copy_firefox_url",
                clip,
            )

            # 4b. Test copy Firefox Add-ons URL button
            copy_and_assert_clipboard(
                dialog._on_copy_firefox_addons_url,
                "about:addons",
                "_on_copy_firefox_addons_url",
                clip,
            )

            # 5. Test link click handler
            copy_and_assert_clipboard(
                lambda: dialog._on_browser_url_clicked("chrome://extensions/"),
                "chrome://extensions/",
                '_on_browser_url_clicked("chrome://extensions/")',
                clip,
            )

            copy_and_assert_clipboard(
                lambda: dialog._on_browser_url_clicked("copy:extension_path"),
                str(EXTENSION_DIR),
                '_on_browser_url_clicked("copy:extension_path")',
                clip,
            )

            copy_and_assert_clipboard(
                lambda: dialog._on_browser_url_clicked("copy:manifest_path"),
                str(EXTENSION_DIR / "manifest.json"),
                '_on_browser_url_clicked("copy:manifest_path")',
                clip,
            )

            # An http(s) link must be handed to the browser, never to the clipboard.
            clip.set_calls.clear()
            opened = []
            monkeypatch.setattr(
                QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
            )
            dialog._on_browser_url_clicked("https://addons.mozilla.org/")
            assert clip.set_calls == [], (
                f"an http(s) link must be opened, not copied; setText calls: "
                f"{clip.set_calls!r}"
            )
            assert len(opened) == 1, f"expected one openUrl call, got {opened!r}"
            assert "addons.mozilla.org" in opened[0]
            clip.set_calls.clear()

            # 6. Test Firefox packaging button. Without the redirect below, the
            # real packager writes browser_extension/my-idm-firefox.xpi into the
            # repository source tree and the call is not asserted at all.
            pkg_spy, produced = patch_packager_into(tmp_path, monkeypatch)
            revealed = record_folder_reveals(monkeypatch)
            information_calls, critical_calls = record_message_boxes(
                monkeypatch, QMessageBox.StandardButton.Ok
            )

            dialog._on_package_firefox_extension()

            assert pkg_spy.call_count == 1, (
                f"_on_package_firefox_extension must package exactly once, got {pkg_spy.call_count}"
            )
            xpi_path = produced["path"]
            assert xpi_path.is_file(), f"the packager produced no .xpi at {xpi_path}"
            assert xpi_path.stat().st_size > 0
            assert xpi_path.is_relative_to(tmp_path), (
                f"packaging must be confined to tmp_path, produced {xpi_path}"
            )
            assert len(information_calls) == 1, (
                f"exactly one success dialog expected, got {information_calls!r}"
            )
            assert information_calls[0][1] == "Firefox Package Created", (
                f"unexpected success dialog title: {information_calls[0][1]!r}"
            )
            assert str(xpi_path) in information_calls[0][2], (
                "the success dialog must show the generated .xpi path, got "
                f"{information_calls[0][2]!r}"
            )
            assert not critical_calls, (
                f"packaging must not take the error path, but QMessageBox.critical fired: "
                f"{critical_calls!r}"
            )
            # The "Ok" answer must not open Explorer.
            assert revealed == [], (
                f"answering Ok must not reveal the folder, show_in_folder got {revealed!r}"
            )
            _assert_repo_xpi_untouched(repo_xpi_before)
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.processEvents()

    def test_browser_package_button_reveals_folder_on_open_answer(
        self, monkeypatch, tmp_path
    ):
        """Choosing "Open" in the packaging dialog must reveal the generated .xpi."""
        repo_xpi_before = _no_stray_repo_xpi()
        dialog = SettingsDialog(
            browser_config=BrowserIntegrationConfig(enabled=True), initial_tab=2
        )
        try:
            pkg_spy, produced = patch_packager_into(tmp_path, monkeypatch)
            revealed = record_folder_reveals(monkeypatch)
            record_message_boxes(monkeypatch, QMessageBox.StandardButton.Open)

            dialog._on_package_firefox_extension()

            assert pkg_spy.call_count == 1, (
                f"expected exactly one packaging call, got {pkg_spy.call_count}"
            )
            assert produced["path"].is_file()
            assert len(revealed) == 1, (
                f"answering Open must reveal the folder exactly once, got {revealed!r}"
            )
            assert revealed[0] == produced["path"], (
                f"show_in_folder must receive the generated .xpi path, got {revealed[0]!r}"
            )
            _assert_repo_xpi_untouched(repo_xpi_before)
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.processEvents()

    def test_browser_package_button_reports_packaging_failure(
        self, monkeypatch, tmp_path
    ):
        """A packager failure must surface as a critical dialog, not a silent pass."""
        dialog = SettingsDialog(
            browser_config=BrowserIntegrationConfig(enabled=True), initial_tab=2
        )
        try:
            def _boom():
                raise OSError("no extension source")

            monkeypatch.setattr(
                "my_idm.extension_packager.package_firefox_extension", _boom
            )
            information_calls, critical_calls = record_message_boxes(
                monkeypatch, QMessageBox.StandardButton.Ok, allow_critical=True
            )

            dialog._on_package_firefox_extension()

            assert not information_calls, (
                f"no success dialog may be shown when packaging raises, got {information_calls!r}"
            )
            assert len(critical_calls) == 1, (
                f"packaging failure must raise exactly one critical dialog, "
                f"got {critical_calls!r}"
            )
            assert critical_calls[0][1] == "Packaging Error", (
                f"unexpected failure dialog title: {critical_calls[0][1]!r}"
            )
            assert "no extension source" in critical_calls[0][2], (
                f"the error text must reach the user, got {critical_calls[0][2]!r}"
            )
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.processEvents()


class TestBrowserExtensionFiles:
    """Verify presence and validity of extension files in browser_extension/."""

    def test_extension_folder_structure(self):
        ext_dir = EXTENSION_DIR
        assert ext_dir.is_dir(), f"bundled extension directory is missing: {ext_dir}"

        manifest_path = ext_dir / "manifest.json"
        assert manifest_path.is_file(), f"missing manifest at {manifest_path}"

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        assert manifest.get("manifest_version") == 3
        assert "downloads" in manifest.get("permissions", []), (
            "the extension needs the downloads permission to intercept"
        )
        assert "cookies" in manifest.get("permissions", []), (
            "the extension needs the cookies permission to forward session state"
        )
        assert "background" in manifest

        # Firefox compatibility checks in main manifest.json
        assert "service_worker" in manifest["background"], (
            "Chromium builds need the MV3 service worker entry"
        )
        assert "scripts" in manifest["background"], (
            "Firefox builds need the MV3 background scripts entry alongside it"
        )
        assert "background.js" in manifest["background"]["scripts"]
        assert "browser_specific_settings" in manifest
        assert "gecko" in manifest["browser_specific_settings"]
        assert manifest["browser_specific_settings"]["gecko"].get("id") == "my-idm@local"
        assert manifest["browser_specific_settings"]["gecko"].get("strict_min_version") == "109.0"

        # Content script checks
        assert "content_scripts" in manifest
        assert any("content.js" in cs.get("js", []) for cs in manifest["content_scripts"]), (
            "content.js must be registered as a content script"
        )

        for name in (
            "background.js",
            "content.js",
            "popup.html",
            "popup.js",
            "options.html",
            "options.js",
            "styles.css",
        ):
            assert (ext_dir / name).is_file(), f"missing extension asset: {name}"

        # Dedicated Firefox manifest check
        firefox_manifest_path = ext_dir / "manifest.firefox.json"
        assert firefox_manifest_path.is_file(), f"missing {firefox_manifest_path}"
        with open(firefox_manifest_path, "r", encoding="utf-8") as f:
            ff_manifest = json.load(f)
        assert ff_manifest.get("manifest_version") == 3
        assert "scripts" in ff_manifest.get("background", {}), (
            "the Firefox-only manifest must not declare a service_worker"
        )
        assert "service_worker" not in ff_manifest.get("background", {}), (
            "Firefox rejects MV3 service_worker; it must live only in manifest.json"
        )
        assert ff_manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id") == "my-idm@local"

        icons_dir = ext_dir / "icons"
        for icon in ("icon16.png", "icon48.png", "icon128.png"):
            assert (icons_dir / icon).is_file(), f"missing extension icon: {icon}"


class TestFirefoxExtensionPackaging:
    """Verify Firefox .xpi packaging logic and installation guide dialog."""

    def test_package_firefox_extension(self, tmp_path):
        import zipfile
        from my_idm.extension_packager import get_default_extension_dir, package_firefox_extension

        assert get_default_extension_dir() == EXTENSION_DIR, (
            "the default extension dir must be the repo's browser_extension/, got "
            f"{get_default_extension_dir()}"
        )

        out_xpi = tmp_path / "test_addon.xpi"
        result_path = package_firefox_extension(output_path=out_xpi)

        assert result_path == out_xpi
        assert out_xpi.is_file()
        assert out_xpi.stat().st_size > 0

        with zipfile.ZipFile(out_xpi, "r") as zf:
            namelist = zf.namelist()
            assert "manifest.json" in namelist
            assert "manifest.firefox.json" not in namelist, (
                "the Firefox manifest is renamed into the archive, not shipped as-is"
            )
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
            assert not any(n.endswith(".xpi") for n in namelist), (
                "a previously built .xpi must not be nested inside the new archive"
            )
            assert not any(n.endswith(".zip") for n in namelist), (
                "a previously built .zip must not be nested inside the archive"
            )

            # Verify manifest content is tailored for Firefox MV3
            with zf.open("manifest.json") as mf:
                manifest_data = json.load(mf)
            assert manifest_data.get("manifest_version") == 3
            assert "scripts" in manifest_data.get("background", {})
            assert manifest_data.get("browser_specific_settings", {}).get("gecko", {}).get("id") == "my-idm@local"

    def test_package_firefox_extension_creates_missing_output_dir(self, tmp_path):
        """The packager creates the archive's parent directory if it is missing."""
        from my_idm.extension_packager import package_firefox_extension

        out_xpi = tmp_path / "nested" / "deeper" / "addon.xpi"
        result_path = package_firefox_extension(output_path=out_xpi)
        assert result_path == out_xpi
        assert out_xpi.is_file(), "the packager must mkdir -p the output directory"

    def test_package_firefox_extension_rejects_missing_source(self, tmp_path):
        """A missing extension directory is a hard error, not a silent no-op."""
        from my_idm.extension_packager import package_firefox_extension

        with pytest.raises(FileNotFoundError) as excinfo:
            package_firefox_extension(
                extension_dir=tmp_path / "no_such_dir",
                output_path=tmp_path / "out.xpi",
            )
        assert "not found" in str(excinfo.value).lower(), (
            f"the error must name the missing directory, got {excinfo.value!r}"
        )
        assert not (tmp_path / "out.xpi").exists(), (
            "no archive may be produced when the source directory is missing"
        )

    def test_firefox_install_guide_dialog(self, monkeypatch, tmp_path):
        repo_xpi_before = _no_stray_repo_xpi()
        opened_urls = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened_urls.append(url.toString()))
        clip = install_fake_clipboard(monkeypatch)

        dialog = FirefoxInstallGuideDialog()
        try:
            # Test copying about:config and about:addons
            copy_and_assert_clipboard(
                lambda: dialog._copy_text("about:config", "test"),
                "about:config",
                '_copy_text("about:config")',
                clip,
            )

            copy_and_assert_clipboard(
                lambda: dialog._copy_text("about:addons", "test"),
                "about:addons",
                '_copy_text("about:addons")',
                clip,
            )

            # Test link click handler for non-http
            copy_and_assert_clipboard(
                lambda: dialog._on_link_clicked("about:config"),
                "about:config",
                '_on_link_clicked("about:config")',
                clip,
            )

            copy_and_assert_clipboard(
                lambda: dialog._on_link_clicked("xpinstall.signatures.required"),
                "xpinstall.signatures.required",
                '_on_link_clicked("xpinstall.signatures.required")',
                clip,
            )

            copy_and_assert_clipboard(
                lambda: dialog._on_link_clicked("false"),
                "false",
                '_on_link_clicked("false")',
                clip,
            )

            copy_and_assert_clipboard(
                lambda: dialog._on_link_clicked("copy:xpi_path"),
                str(EXTENSION_DIR / "my-idm-firefox.xpi"),
                '_on_link_clicked("copy:xpi_path")',
                clip,
            )

            # Test external link routing to QDesktopServices
            dialog._on_link_clicked("https://addons.mozilla.org/developers/addon/submit/distribution")
            assert len(opened_urls) == 1, (
                f"an https link must be handed to QDesktopServices exactly once, got {opened_urls!r}"
            )
            assert "addons.mozilla.org" in opened_urls[0]

            # Test packaging triggered from guide. Redirected into tmp_path so the
            # repository source tree is not polluted with a real .xpi.
            pkg_spy, produced = patch_packager_into(tmp_path, monkeypatch)
            revealed = record_folder_reveals(monkeypatch)
            information_calls, critical_calls = record_message_boxes(
                monkeypatch, QMessageBox.StandardButton.Open
            )

            dialog._on_package_clicked()

            assert pkg_spy.call_count == 1, (
                f"_on_package_clicked must package exactly once, got {pkg_spy.call_count}"
            )
            assert produced["path"].is_file(), (
                f"the guide must produce a real .xpi, missing {produced['path']}"
            )
            assert not critical_calls, (
                f"packaging must not take the error path, got {critical_calls!r}"
            )
            assert len(information_calls) == 1, (
                f"exactly one success dialog expected, got {information_calls!r}"
            )
            assert information_calls[0][1] == "Package Created", (
                f"unexpected success dialog title: {information_calls[0][1]!r}"
            )
            assert str(produced["path"]) in information_calls[0][2], (
                "the success dialog must show the generated .xpi path, got "
                f"{information_calls[0][2]!r}"
            )
            assert len(revealed) == 1, (
                f"answering Open must reveal the folder exactly once, got {revealed!r}"
            )
            assert revealed[0] == produced["path"], (
                f"show_in_folder must receive the generated .xpi, got {revealed[0]!r}"
            )
            _assert_repo_xpi_untouched(repo_xpi_before)
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.processEvents()

    def test_firefox_guide_open_folder_targets_xpi_when_present(self, monkeypatch, tmp_path):
        """_on_open_folder_clicked reveals the .xpi if it exists, else the folder."""
        dialog = FirefoxInstallGuideDialog()
        try:
            revealed = record_folder_reveals(monkeypatch)
            xpi = EXTENSION_DIR / "my-idm-firefox.xpi"
            expected = xpi if xpi.is_file() else EXTENSION_DIR
            dialog._on_open_folder_clicked()
            assert len(revealed) == 1, f"expected one reveal, got {revealed!r}"
            assert revealed[0] == expected, (
                f"open-folder must target {expected!r}, got {revealed[0]!r}"
            )
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.processEvents()


class TestEdgeBrowserIntegration:
    """Test Microsoft Edge specific browser integration workflows and compatibility."""

    def test_edge_download_metadata_and_headers(self, tmp_path):
        db_path = tmp_path / "test_edge.db"
        db = Database(str(db_path))
        db.open()

        manager = DownloadManager(db)
        try:
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

            assert isinstance(did, str) and did, (
                f"Edge download must yield a non-empty id, got {did!r}"
            )
            entry = db.get_download(did)
            assert entry is not None, f"no database row persisted for {did!r}"
            assert entry.url == "https://edge.microsoft.com/test_setup.exe"
            assert entry.filename == "test_setup.exe"
            assert Path(entry.save_path) == tmp_path, (
                f"the Edge save_path must be persisted, got {entry.save_path!r}"
            )
            assert entry.metadata.get("source") == "browser_extension"
            assert entry.metadata.get("user_agent") == edge_ua, (
                "the Edge UA must be stored verbatim so the server can impersonate it"
            )
            assert entry.metadata.get("referer") == "https://www.microsoft.com/edge"
            edge_headers = entry.metadata["headers"]
            assert "Edg/128.0.0.0" in edge_headers["User-Agent"], (
                f"User-Agent header lost the Edg token: {edge_headers['User-Agent']!r}"
            )
            assert edge_headers["Sec-CH-UA"] == '"Microsoft Edge";v="128"'
            assert edge_headers.get("Cookie") == "edge_session=edg123;", (
                "a raw cookie string must be passed through unchanged, got "
                f"{edge_headers.get('Cookie')!r}"
            )
            assert edge_headers.get("Referer") == "https://www.microsoft.com/edge"
        finally:
            manager.stop()
            db.close()

    def test_edge_url_copying_and_links(self, monkeypatch):
        clip = install_fake_clipboard(monkeypatch)
        dialog = SettingsDialog(
            browser_config=BrowserIntegrationConfig(enabled=True), initial_tab=2
        )
        try:
            # 1. Direct Edge copy method
            copy_and_assert_clipboard(
                dialog._on_copy_edge_url, "edge://extensions/", "_on_copy_edge_url", clip
            )

            # 2. Clicking edge://extensions/ URL link
            copy_and_assert_clipboard(
                lambda: dialog._on_browser_url_clicked("edge://extensions/"),
                "edge://extensions/",
                '_on_browser_url_clicked("edge://extensions/")',
                clip,
            )

            # 3. Label text and group title contain Edge references
            raw_html = dialog._chrome_instr_lbl.text()
            assert "edge://extensions/" in raw_html, (
                "the Chromium instructions must also cover Edge, which ships the same "
                f"extension store. Label text: {raw_html!r}"
            )
            assert "Edge" in dialog._chrome_group.title(), (
                f"group title must mention Edge, got {dialog._chrome_group.title()!r}"
            )
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.processEvents()


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
            with mock.patch("aiohttp.ClientSession", return_value=MockSession()):
                length = await server._probe_content_length("https://example.com/bigfile.bin")
                assert length == 10485760, (
                    "a 200 HEAD with a non-HTML Content-Length must be trusted verbatim, "
                    f"got {length!r}"
                )

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
            with mock.patch("aiohttp.ClientSession", return_value=MockSession()):
                length = await server._probe_content_length("https://example.com/ranged_file.zip")
                assert length == 5242880, (
                    "a 405 HEAD must fall back to a bytes=0-0 GET and read the total "
                    f"from Content-Range, got {length!r}"
                )

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
            with mock.patch("aiohttp.ClientSession", return_value=MockSession()):
                length = await server._probe_content_length("https://example.com/download_page")
                assert length is None, (
                    "an HTML landing page is not the file; reporting its Content-Length "
                    f"would wrongly accept a tiny download, got {length!r}"
                )

        asyncio.run(_test())

    def test_magnet_link_bypasses_size_threshold(self):
        mock_manager = MagicMock()
        mock_manager.add_download_from_browser.return_value = "mag-999"

        test_port = free_tcp_port()

        async def _run():
            # Configure 10,000 KB minimum size
            cfg = BrowserIntegrationConfig(enabled=True, port=test_port, min_file_size_kb=10000)
            server = BrowserServer(mock_manager, cfg)

            started = await server.start()
            assert started is True, f"BrowserServer failed to bind 127.0.0.1:{test_port}"

            try:
                async with aiohttp.ClientSession() as session:
                    magnet_payload = {
                        "url": "magnet:?xt=urn:btih:d1234567890abcdef1234567890abcdef1234567&dn=TestTorrent",
                        "filename": "TestTorrent",
                    }
                    async with session.post(f"http://127.0.0.1:{test_port}/add", json=magnet_payload) as resp:
                        assert resp.status == 200
                        data = await resp.json()
                        assert data["status"] == "ok", (
                            "a magnet link has no content length and must never be dropped "
                            f"by the size filter, got {data!r}"
                        )
                        assert data["id"] == "mag-999"

                    mock_manager.add_download_from_browser.assert_called_once()
                    assert mock_manager.add_download_from_browser.call_args.kwargs["url"].startswith(
                        "magnet:"
                    ), "the magnet URL must be forwarded to the manager unchanged"
            finally:
                await server.stop()
                assert server.is_running is False

        asyncio.run(_run())
        assert_port_released(test_port, "magnet link test")
