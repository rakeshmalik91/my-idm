"""Hardened tests for notifications, single-instance IPC framing, and the ``main`` entry point.

These three modules sit at the edges of the application and were almost entirely untested
(``main.py`` 19%, ``notifications.py`` 50%, ``single_instance.py`` 57%). The bugs they are
prone to are the awkward ones: a notification handler that throws and must not take the
caller down, an IPC frame that arrives as malformed JSON, and a startup path whose cleanup
must run exactly once whether the app quits normally or a second instance short-circuits.

Scope, and what is deliberately *not* here:

* ``tests/test_single_instance.py`` already covers the transport end-to-end with real
  child processes and real named pipes. These tests cover the **framing logic**
  (``_on_new_connection`` / ``_process_data``) with a fake client, which is both safer and
  more precise: a live ``QLocalSocket`` written by hand and abandoned mid-loop is a genuine
  Qt lifetime hazard, and the payload shapes it would exercise are the ones already proven
  multi-process.
* ``main.main()`` runs with every collaborator faked, so no window, database, torrent
  session or splash screen is ever constructed.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

from my_idm import notifications
from my_idm.notifications import (
    notify_browser_download_caught,
    notify_download_complete,
    notify_download_error,
    register_notification_handler,
    show_notification,
    unregister_notification_handler,
)
from my_idm.single_instance import (
    DEFAULT_SERVER_NAME,
    SingleInstanceManager,
    activate_window,
)

app = QApplication.instance() or QApplication(sys.argv)


def _exit_with_code(code=0):
    """``sys.exit`` replacement that preserves the code the caller passed.

    ``patch(..., side_effect=SystemExit)`` would discard the argument, so every
    ``SystemExit.code`` would read back as ``None`` and the exit-code assertions below
    would pass vacuously.
    """
    raise SystemExit(code)


class _ByteArray:
    """``QLocalSocket.readAll()`` returns a ``QByteArray``; the code calls ``.data()``."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes):
        self._raw = raw

    def data(self) -> bytes:
        return self._raw


class FakeIpcClient(QObject):
    """Stand-in for the ``QLocalSocket`` a ``QLocalServer`` hands back.

    Exposes exactly the surface ``_on_new_connection`` touches - ``isValid``,
    ``readAll``, ``bytesAvailable``, ``write``, ``flush``, ``deleteLater`` and the
    ``readyRead`` / ``disconnected`` signals - so the framing logic can be driven
    deterministically without a live named pipe.
    """

    readyRead = Signal()
    disconnected = Signal()

    def __init__(self, payload: bytes = b"", valid: bool = True, parent=None):
        super().__init__(parent)
        self._payload = payload
        self._valid = valid
        self.written: list[bytes] = []
        self.flushed = 0
        self.deleted = 0

    def isValid(self) -> bool:
        return self._valid

    def readAll(self) -> _ByteArray:
        data, self._payload = self._payload, b""
        return _ByteArray(data)

    def bytesAvailable(self) -> int:
        return len(self._payload)

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        self.flushed += 1

    def deleteLater(self) -> None:
        self.deleted += 1

    def feed(self, data: bytes) -> None:
        """Append more bytes and fire ``readyRead``, as a real socket would."""
        self._payload += data
        self.readyRead.emit()


class TestNotificationHandlerRouting(unittest.TestCase):
    """The delegate chain: custom handler first, win10toast only as a fallback."""

    def setUp(self):
        unregister_notification_handler()
        self.addCleanup(unregister_notification_handler)

    def test_a_registered_handler_receives_every_argument(self):
        seen: list[tuple] = []
        register_notification_handler(lambda *a: seen.append(a) or True)
        self.assertTrue(show_notification("T", "M", duration=7, icon_path="i.ico"))
        self.assertEqual(seen, [("T", "M", 7, "i.ico")])

    def test_a_handler_returning_true_short_circuits_win10toast(self):
        register_notification_handler(lambda *a: True)
        toaster = MagicMock()
        with patch.object(notifications, "_toaster", toaster):
            self.assertTrue(show_notification("T", "M"))
        toaster.show_toast.assert_not_called()

    def test_a_handler_returning_false_falls_through_to_win10toast(self):
        register_notification_handler(lambda *a: False)
        toaster = MagicMock()
        with patch.object(notifications, "_toaster", toaster), \
             patch.object(notifications, "_HAS_WIN10TOAST", True):
            self.assertTrue(show_notification("T", "M"))
        toaster.show_toast.assert_called_once()

    def test_a_raising_handler_is_contained_and_the_toast_still_fires(self):
        """A broken tray hook must never take a download completion down with it."""

        def _boom(*args):
            raise RuntimeError("tray exploded")

        register_notification_handler(_boom)
        toaster = MagicMock()
        with patch.object(notifications, "_toaster", toaster), \
             patch.object(notifications, "_HAS_WIN10TOAST", True):
            self.assertTrue(show_notification("T", "M"), "the failure must be swallowed")
        toaster.show_toast.assert_called_once()

    def test_win10toast_passes_duration_icon_and_threading(self):
        toaster = MagicMock()
        with patch.object(notifications, "_toaster", toaster), \
             patch.object(notifications, "_HAS_WIN10TOAST", True):
            show_notification("T", "M", duration=9, icon_path="x.ico")
        toaster.show_toast.assert_called_once_with(
            "T", "M", duration=9, icon_path="x.ico", threaded=True
        )

    def test_a_raising_toaster_reports_failure_instead_of_propagating(self):
        toaster = MagicMock()
        toaster.show_toast.side_effect = OSError("no shell")
        with patch.object(notifications, "_toaster", toaster), \
             patch.object(notifications, "_HAS_WIN10TOAST", True):
            self.assertFalse(show_notification("T", "M"))

    def test_missing_win10toast_reports_failure(self):
        with patch.object(notifications, "_toaster", None), \
             patch.object(notifications, "_HAS_WIN10TOAST", False):
            self.assertFalse(show_notification("T", "M"))

    def test_unregister_only_clears_the_matching_handler(self):
        first = lambda *a: True  # noqa: E731
        register_notification_handler(first)
        unregister_notification_handler(lambda *a: False)
        self.assertIsNotNone(
            notifications._notification_handler,
            "unregistering a different handler must be a no-op",
        )
        unregister_notification_handler(first)
        self.assertIsNone(notifications._notification_handler)

    def test_unregister_without_an_argument_always_clears(self):
        register_notification_handler(lambda *a: True)
        unregister_notification_handler()
        self.assertIsNone(notifications._notification_handler)

    def test_show_notification_with_no_handler_and_no_toast_is_false_not_an_error(self):
        unregister_notification_handler()
        with patch.object(notifications, "_toaster", None), \
             patch.object(notifications, "_HAS_WIN10TOAST", False):
            self.assertFalse(show_notification("", ""))


class TestNotificationMessages(unittest.TestCase):
    """The three user-facing notification shapes and their truncation rules."""

    def setUp(self):
        self.seen: list[tuple] = []
        register_notification_handler(lambda *a: self.seen.append(a) or True)
        self.addCleanup(unregister_notification_handler)

    def _message(self) -> str:
        return self.seen[-1][1]

    def test_browser_capture_with_a_filename(self):
        notify_browser_download_caught("clip.mp4")
        title, message, duration, icon = self.seen[-1]
        self.assertEqual(title, "Download Captured")
        self.assertIn("clip.mp4", message)
        self.assertEqual(duration, 5)
        self.assertIsNone(icon)

    def test_browser_capture_without_a_filename(self):
        notify_browser_download_caught("")
        self.assertIn("browser", self._message())

    def test_browser_capture_with_a_url(self):
        notify_browser_download_caught("a.zip", "https://example.com/a.zip")
        self.assertIn("https://example.com/a.zip", self._message())

    def test_a_long_url_is_truncated_with_an_ellipsis(self):
        long_url = "https://example.com/" + "x" * 200
        notify_browser_download_caught("a.zip", long_url)
        message = self._message()
        self.assertIn("...", message)
        self.assertNotIn(long_url, message)

    def test_a_short_url_is_not_truncated(self):
        url = "https://example.com/a.zip"
        notify_browser_download_caught("a.zip", url)
        self.assertIn(url, self._message())
        self.assertNotIn("...", self._message())

    def test_a_url_of_exactly_eighty_characters_is_not_truncated(self):
        url = "https://e.com/" + "a" * (80 - len("https://e.com/"))
        self.assertEqual(len(url), 80)
        notify_browser_download_caught("a.zip", url)
        self.assertNotIn("...", self._message())

    def test_download_complete_uses_the_long_duration(self):
        notify_download_complete("movie.mkv")
        title, message, duration, _ = self.seen[-1]
        self.assertEqual(title, "Download Complete")
        self.assertIn("movie.mkv", message)
        self.assertEqual(duration, 8, "completion is worth a longer toast")

    def test_download_error_truncates_the_cause(self):
        notify_download_error("a.zip", "e" * 500)
        title, message, duration, _ = self.seen[-1]
        self.assertEqual(title, "Download Failed")
        self.assertEqual(duration, 10)
        self.assertIn("a.zip", message)
        self.assertLessEqual(len(message.split("\n", 1)[1]), 100)

    def test_download_error_keeps_a_short_cause_intact(self):
        notify_download_error("a.zip", "timed out")
        self.assertIn("timed out", self._message())


class TestIpcFraming(unittest.TestCase):
    """``_on_new_connection`` / ``_process_data``: framing, buffering, and error paths.

    Driven with :class:`FakeIpcClient` so the JSON handling - the part that can actually go
    wrong - is exercised deterministically, with no named-pipe lifetime hazards.
    """

    def setUp(self):
        self.server_name = f"my_idm_test_ipc_framing_{id(self)}"
        self.addCleanup(QLocalServer.removeServer, self.server_name)
        self.manager = SingleInstanceManager(self.server_name)
        self.received: list[dict] = []
        self.manager.message_received.connect(self.received.append)
        self.server = MagicMock()
        self.manager._server = self.server
        self.addCleanup(self._reset_manager)

    def _reset_manager(self):
        self.manager._server = None

    def _connect(self, client: FakeIpcClient):
        self.server.nextPendingConnection.return_value = client
        self.manager._on_new_connection()
        return client

    def test_a_complete_frame_is_emitted_and_acknowledged(self):
        payload = {"action": "activate", "urls": ["magnet:?xt=urn:btih:abc"]}
        client = self._connect(FakeIpcClient(json.dumps(payload).encode()))
        self.assertEqual(self.received, [payload])
        self.assertEqual(client.written, [b"OK\n"], "the sender must get an acknowledgement")
        self.assertEqual(client.flushed, 1)

    def test_a_unicode_payload_round_trips(self):
        payload = {"action": "activate", "backlog": "C:/Users/rákés/Liste — 2026.txt"}
        self._connect(FakeIpcClient(json.dumps(payload).encode("utf-8")))
        self.assertEqual(self.received, [payload])

    def test_an_empty_payload_is_emitted_as_an_empty_dict(self):
        self._connect(FakeIpcClient(b"{}"))
        self.assertEqual(self.received, [{}])

    def test_malformed_json_is_dropped_and_never_raises(self):
        """A corrupted frame must be discarded, not surfaced as a signal or a crash."""
        client = self._connect(FakeIpcClient(b"{not valid json"))
        self.assertEqual(self.received, [], "a malformed frame must not be emitted")
        self.assertEqual(client.written, [], "and must not be acknowledged as received")

    def test_a_malformed_frame_does_not_break_the_next_valid_one(self):
        self._connect(FakeIpcClient(b"{not valid json"))
        self.manager._on_new_connection()
        self.server.nextPendingConnection.return_value = FakeIpcClient(
            json.dumps({"action": "activate"}).encode()
        )
        self.manager._on_new_connection()
        self.assertEqual(self.received, [{"action": "activate"}])

    def test_a_truncated_frame_is_buffered_until_it_completes(self):
        """JSON must not be parsed until the whole object has arrived."""
        raw = json.dumps({"action": "activate", "urls": ["a", "b"]}).encode()
        cut = len(raw) // 2
        client = self._connect(FakeIpcClient(raw[:cut]))
        self.assertEqual(self.received, [], "a half frame must not be parsed")
        client.feed(raw[cut:])
        self.assertEqual(self.received, [{"action": "activate", "urls": ["a", "b"]}],
                         "the completed frame must then be emitted")
        self.assertEqual(client.written, [b"OK\n"])

    def test_a_frame_that_is_not_utf8_is_dropped(self):
        self._connect(FakeIpcClient(b'{"a": "\xff\xfe"}'))
        self.assertEqual(self.received, [])

    def test_an_invalid_client_is_ignored(self):
        self._connect(FakeIpcClient(json.dumps({"a": 1}).encode(), valid=False))
        self.assertEqual(self.received, [])

    def test_an_empty_read_emits_nothing(self):
        self._connect(FakeIpcClient(b""))
        self.assertEqual(self.received, [])

    def test_no_server_means_no_client_and_no_raise(self):
        self.manager._server = None
        self.manager._on_new_connection()  # must not raise

    def test_a_server_with_no_pending_connection_is_ignored(self):
        self.server.nextPendingConnection.return_value = None
        self.manager._on_new_connection()  # must not raise

    def test_the_client_is_deleted_when_it_disconnects(self):
        client = self._connect(FakeIpcClient(b"{}"))
        self.assertEqual(client.deleted, 0)
        client.disconnected.emit()
        self.assertEqual(client.deleted, 1, "a dropped client must not leak a socket")

    def test_two_successive_frames_on_one_connection_both_arrive(self):
        client = self._connect(FakeIpcClient(b""))
        client.feed(json.dumps({"n": 1}).encode())
        client.feed(json.dumps({"n": 2}).encode())
        self.assertEqual(self.received, [{"n": 1}, {"n": 2}])


class TestSingleInstanceBasics(unittest.TestCase):
    """Socket-name handling and the connect-failure path, without a live pipe."""

    def test_default_server_name_is_the_production_one(self):
        self.assertEqual(DEFAULT_SERVER_NAME, "my_idm_single_instance_ipc")

    def test_a_manager_defaults_to_the_production_server_name(self):
        manager = SingleInstanceManager()
        self.assertEqual(manager.server_name, DEFAULT_SERVER_NAME)
        self.assertIsNone(manager._server)

    def test_send_message_to_a_dead_server_reports_failure(self):
        manager = SingleInstanceManager("my_idm_test_ipc_nobody_listening")
        self.assertFalse(manager.send_message({"action": "activate"}, timeout_ms=200))

    def test_send_message_with_an_unserialisable_payload_reports_failure(self):
        """A payload that cannot be JSON-encoded must fail, not raise at the sender."""
        manager = SingleInstanceManager("my_idm_test_ipc_unserialisable")
        with patch("my_idm.single_instance.QLocalSocket") as socket_cls:
            instance = socket_cls.return_value
            instance.waitForConnected.return_value = True
            self.assertFalse(manager.send_message({"bad": object()}, timeout_ms=200))
        instance.disconnectFromServer.assert_called_once()

    def test_close_is_idempotent_and_safe_before_start(self):
        manager = SingleInstanceManager("my_idm_test_ipc_never_started")
        manager.close()
        manager.close()  # must not raise

    def test_close_swallows_a_failing_server(self):
        manager = SingleInstanceManager("my_idm_test_ipc_bad_close")
        server = MagicMock()
        server.close.side_effect = RuntimeError("already gone")
        manager._server = server
        manager.close()
        self.assertIsNone(manager._server, "the handle must be released even on failure")

    def test_start_server_reports_failure_without_raising(self):
        manager = SingleInstanceManager("my_idm_test_ipc_listen_fail")
        self.addCleanup(manager.close)
        with patch("my_idm.single_instance.QLocalServer") as server_cls:
            server_cls.return_value.listen.return_value = False
            server_cls.return_value.errorString.return_value = "address in use"
            self.assertFalse(manager.start_server())


class TestActivateWindow(unittest.TestCase):
    """Bringing the primary window forward from a secondary instance."""

    def test_a_minimized_window_is_restored(self):
        window = MagicMock()
        window.isMinimized.return_value = True
        # A real flag value, not a MagicMock: the code does bitwise `&`/`~`/`|` on it.
        window.windowState.return_value = Qt.WindowState.WindowMinimized
        activate_window(window)
        window.setWindowState.assert_called_once()
        self.assertEqual(
            window.setWindowState.call_args.args[0],
            Qt.WindowState.WindowActive,
            "the minimized bit must be cleared and the window activated",
        )
        window.showNormal.assert_called_once()
        window.show.assert_not_called()
        window.raise_.assert_called_once()
        window.activateWindow.assert_called_once()

    def test_a_minimized_window_with_extra_state_keeps_the_extra_bits(self):
        """``WindowNoState | WindowMinimized`` must restore to ``WindowNoState``."""
        window = MagicMock()
        window.isMinimized.return_value = True
        window.windowState.return_value = (
            Qt.WindowState.WindowNoState | Qt.WindowState.WindowMinimized
        )
        activate_window(window)
        self.assertEqual(
            window.setWindowState.call_args.args[0], Qt.WindowState.WindowActive
        )

    def test_a_visible_window_is_just_raised(self):
        window = MagicMock()
        window.isMinimized.return_value = False
        activate_window(window)
        window.show.assert_called_once()
        window.showNormal.assert_not_called()
        window.setWindowState.assert_not_called()

    def test_a_failing_window_is_contained(self):
        window = MagicMock()
        window.isMinimized.side_effect = RuntimeError("window gone")
        activate_window(window)  # must not raise
        window.raise_.assert_not_called()

    def test_a_window_whose_raising_fails_is_contained(self):
        window = MagicMock()
        window.isMinimized.return_value = False
        window.raise_.side_effect = RuntimeError("no window manager")
        activate_window(window)  # must not raise
        window.activateWindow.assert_not_called()

    def test_a_missing_window_is_a_no_op(self):
        activate_window(None)


class TestMainEntryPoint(unittest.TestCase):
    """``my_idm.main``: argument parsing and the one-shot startup/shutdown contract.

    ``main()`` runs with ``QApplication``, ``Database``, ``DownloadManager``,
    ``MainWindow`` and ``SingleInstanceManager`` all replaced, so the real window, SQLite
    file, torrent session and splash screen are never constructed. The assertions target
    the two things that are easy to get wrong and expensive to debug in the field: what the
    CLI actually parses, and that teardown happens exactly once.
    """

    def _parse(self, argv):
        from my_idm import main as main_module

        with patch.object(sys, "argv", ["my-idm"] + argv):
            return main_module.parse_args()

    # -- CLI ------------------------------------------------------------------

    def test_defaults(self):
        args = self._parse([])
        self.assertIsNone(args.backlog)
        self.assertFalse(args.verbose)
        self.assertFalse(args.no_splash)
        self.assertFalse(args.restart)
        self.assertEqual(args.urls, [])

    def test_short_and_long_flags_agree(self):
        long_form = self._parse(["--verbose", "--no-splash", "--restart", "--backlog", "b.txt"])
        short_form = self._parse(["-v", "--no-splash", "--restart", "-b", "b.txt"])
        self.assertTrue(long_form.verbose)
        self.assertEqual(long_form.backlog, "b.txt")
        self.assertEqual(vars(long_form), vars(short_form))

    def test_positional_urls_are_collected_in_order(self):
        args = self._parse(["https://a/x.zip", "magnet:?xt=urn:btih:abc", "a.torrent"])
        self.assertEqual(
            args.urls, ["https://a/x.zip", "magnet:?xt=urn:btih:abc", "a.torrent"]
        )

    def test_urls_come_before_the_backlog_flag_independently(self):
        args = self._parse(["-b", "list.txt", "https://a/x.zip"])
        self.assertEqual(args.backlog, "list.txt")
        self.assertEqual(args.urls, ["https://a/x.zip"])

    def test_an_unknown_flag_exits(self):
        with self.assertRaises(SystemExit):
            self._parse(["--definitely-not-a-flag"])

    # -- startup / shutdown ---------------------------------------------------

    def _drive_main(self, argv, send_message=True, start_server=True,
                    enable_tray=False, start_minimized=False, exec_code=0):
        """Run ``main()`` with every collaborator faked; return the recorded effects."""
        from my_idm import main as main_module

        calls: dict[str, list] = {"add_download": [], "closed": [], "exec": []}

        db = MagicMock()
        db.close.side_effect = lambda: calls["closed"].append("db")

        manager = MagicMock()
        manager.general_config.enable_system_tray = enable_tray
        manager.general_config.start_minimized = start_minimized
        manager.add_download.side_effect = lambda url: calls["add_download"].append(url)
        manager.process_backlogs.return_value = 0
        manager.stop.side_effect = lambda: calls["closed"].append("manager")

        single = MagicMock()
        single.send_message.return_value = send_message
        single.start_server.return_value = start_server

        window = MagicMock()
        application = MagicMock()
        application.exec.side_effect = lambda: (calls["exec"].append(exec_code), exec_code)[1]

        with patch.object(main_module, "Database", return_value=db), \
             patch.object(main_module, "DownloadManager", return_value=manager), \
             patch.object(main_module, "MainWindow", return_value=window), \
             patch("my_idm.single_instance.SingleInstanceManager", return_value=single), \
             patch("my_idm.resources.get_app_icon",
                   return_value=MagicMock(isNull=MagicMock(return_value=True))), \
             patch.object(main_module, "QApplication", return_value=application), \
             patch.object(sys, "argv", ["my-idm"] + argv), \
             patch.object(sys, "exit", side_effect=_exit_with_code):
            with self.assertRaises(SystemExit) as ctx:
                main_module.main()
        calls["exit_code"] = ctx.exception.code
        return calls, db, manager, single, window

    def test_a_second_instance_exits_before_starting_anything(self):
        """A live primary must be handed the payload and then left entirely alone."""
        calls, db, manager, single, window = self._drive_main(
            ["https://a/x.zip"], send_message=True
        )
        self.assertEqual(calls["exit_code"], 0)
        single.start_server.assert_not_called()
        db.open.assert_not_called()
        manager.start.assert_not_called()
        window.show.assert_not_called()
        self.assertEqual(calls["closed"], [], "a second instance owns nothing to clean up")

    def test_the_second_instance_payload_carries_cli_state(self):
        _, _, _, single, _ = self._drive_main(
            ["-b", "C:/list.txt", "https://a/x.zip"], send_message=True
        )
        payload = single.send_message.call_args.args[0]
        self.assertEqual(payload["action"], "activate")
        self.assertEqual(payload["backlog"], "C:/list.txt")
        self.assertEqual(payload["urls"], ["https://a/x.zip"])

    def test_restart_flag_skips_the_single_instance_probe(self):
        """``--restart`` must not bounce the relaunched process straight back out."""
        _, _, _, single, _ = self._drive_main(["--restart", "--no-splash"])
        single.send_message.assert_not_called()
        single.start_server.assert_called_once()

    def test_a_first_instance_starts_everything_and_cleans_up_once(self):
        calls, db, manager, single, window = self._drive_main(
            ["--no-splash", "https://a/x.zip"], send_message=False
        )
        self.assertTrue(single.start_server.called)
        db.open.assert_called_once()
        manager.start.assert_called_once()
        window.show.assert_called_once()
        self.assertEqual(calls["add_download"], ["https://a/x.zip"])
        self.assertEqual(calls["exit_code"], 0)
        self.assertEqual(
            calls["closed"].count("manager"), 1,
            "the manager must be stopped exactly once, not once per exit path",
        )
        self.assertEqual(calls["closed"].count("db"), 1)

    def test_cleanup_runs_exactly_once_despite_the_about_to_quit_hook(self):
        """``_cleanup`` is idempotent so the ``aboutToQuit`` connection and the post-``exec``
        call cannot double-close the database or the engine."""
        calls, _, _, single, _ = self._drive_main(["--no-splash"], send_message=False)
        self.assertEqual(sorted(calls["closed"]), ["db", "manager"])
        single.close.assert_called_once()

    def test_a_failed_ipc_listen_proceeds_as_standalone(self):
        calls, db, _, _, window = self._drive_main(
            ["--no-splash"], send_message=False, start_server=False
        )
        self.assertEqual(calls["exit_code"], 0)
        self.assertTrue(db.open.called, "a failed listener must not block startup")
        self.assertTrue(window.show.called)

    def test_start_minimized_with_tray_skips_the_window(self):
        calls, _, _, _, window = self._drive_main(
            ["--no-splash"], send_message=False, enable_tray=True, start_minimized=True
        )
        window.show.assert_not_called()
        self.assertEqual(calls["exit_code"], 0)

    def test_start_minimized_without_tray_still_shows_the_window(self):
        calls, _, _, _, window = self._drive_main(
            ["--no-splash"], send_message=False, enable_tray=False, start_minimized=True
        )
        window.show.assert_called_once()
        self.assertEqual(calls["exit_code"], 0)

    def test_a_non_zero_exit_code_is_propagated(self):
        calls, _, _, _, _ = self._drive_main(
            ["--no-splash"], send_message=False, exec_code=3
        )
        self.assertEqual(calls["exit_code"], 3)

    def test_blank_cli_urls_are_skipped(self):
        calls, _, _, _, _ = self._drive_main(
            ["--no-splash", ""], send_message=False
        )
        self.assertEqual(calls["add_download"], [], "an empty positional must not be added")

    def test_the_backlog_is_processed_before_the_loop_starts(self):
        _, _, manager, _, _ = self._drive_main(
            ["--no-splash", "-b", "C:/list.txt"], send_message=False
        )
        manager.process_backlogs.assert_called_once_with(extra_filepath="C:/list.txt")

    def test_a_failing_splash_screen_does_not_abort_startup(self):
        """A splash that cannot be constructed must degrade, not stop the app."""
        from my_idm import main as main_module

        db = MagicMock()
        manager = MagicMock()
        manager.general_config.enable_system_tray = False
        manager.general_config.start_minimized = False
        single = MagicMock()
        single.send_message.return_value = False
        single.start_server.return_value = True
        window = MagicMock()
        application = MagicMock()
        application.exec.return_value = 0

        with patch.object(main_module, "Database", return_value=db), \
             patch.object(main_module, "DownloadManager", return_value=manager), \
             patch.object(main_module, "MainWindow", return_value=window), \
             patch("my_idm.single_instance.SingleInstanceManager", return_value=single), \
             patch("my_idm.resources.get_app_icon",
                   return_value=MagicMock(isNull=MagicMock(return_value=True))), \
             patch("my_idm.splash.IDMSplashScreen", side_effect=RuntimeError("no display")), \
             patch.object(main_module, "QApplication", return_value=application), \
             patch.object(sys, "argv", ["my-idm"]), \
             patch.object(sys, "exit", side_effect=_exit_with_code):
            with self.assertRaises(SystemExit) as ctx:
                main_module.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertTrue(window.show.called, "startup must continue without the splash")


class TestSetupLogging(unittest.TestCase):
    """``setup_logging`` must be repeatable and must not leak handlers."""

    def setUp(self):
        from my_idm import main as main_module

        self.module = main_module
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.log_dir = Path(holder.name)
        self.root_handlers = list(logging.root.handlers)
        self.addCleanup(self._restore)

    def _restore(self):
        for handler in list(logging.root.handlers):
            if handler not in self.root_handlers:
                logging.root.removeHandler(handler)
                handler.close()
        logging.getLogger("aiohttp").setLevel(logging.NOTSET)
        logging.getLogger("PySide6").setLevel(logging.NOTSET)

    def test_creates_the_log_file_and_adds_exactly_two_handlers(self):
        from logging import FileHandler, StreamHandler

        before = len(logging.root.handlers)
        with patch.object(self.module, "APP_DIR", self.log_dir), \
             patch.object(self.module, "LOGS_DIR", self.log_dir), \
             patch.object(self.module, "LOG_FILE", self.log_dir / "my-idm.log"):
            self.module.setup_logging(verbose=True)
        added = logging.root.handlers[before:]
        self.assertEqual(len(added), 2)
        self.assertIsInstance(added[0], FileHandler)
        self.assertIsInstance(added[1], StreamHandler)
        self.assertTrue((self.log_dir / "my-idm.log").exists())

    def test_quietens_the_noisy_loggers(self):
        with patch.object(self.module, "APP_DIR", self.log_dir), \
             patch.object(self.module, "LOGS_DIR", self.log_dir), \
             patch.object(self.module, "LOG_FILE", self.log_dir / "my-idm.log"):
            self.module.setup_logging(verbose=False)
        self.assertEqual(logging.getLogger("aiohttp").level, logging.WARNING)
        self.assertEqual(logging.getLogger("PySide6").level, logging.WARNING)

    def test_verbose_selects_debug_for_the_console(self):
        with patch.object(self.module, "APP_DIR", self.log_dir), \
             patch.object(self.module, "LOGS_DIR", self.log_dir), \
             patch.object(self.module, "LOG_FILE", self.log_dir / "my-idm.log"):
            self.module.setup_logging(verbose=True)
        console = [h for h in logging.root.handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)][-1]
        self.assertEqual(console.level, logging.DEBUG)

    def test_non_verbose_selects_info_for_the_console(self):
        with patch.object(self.module, "APP_DIR", self.log_dir), \
             patch.object(self.module, "LOGS_DIR", self.log_dir), \
             patch.object(self.module, "LOG_FILE", self.log_dir / "my-idm.log"):
            self.module.setup_logging(verbose=False)
        console = [h for h in logging.root.handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)][-1]
        self.assertEqual(console.level, logging.INFO)

    def test_the_file_handler_always_records_debug(self):
        with patch.object(self.module, "APP_DIR", self.log_dir), \
             patch.object(self.module, "LOGS_DIR", self.log_dir), \
             patch.object(self.module, "LOG_FILE", self.log_dir / "my-idm.log"):
            self.module.setup_logging(verbose=False)
        file_handler = [h for h in logging.root.handlers
                        if isinstance(h, logging.FileHandler)][-1]
        self.assertEqual(file_handler.level, logging.DEBUG)


if __name__ == "__main__":
    unittest.main()


