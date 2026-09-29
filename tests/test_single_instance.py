"""Unit tests for single instance application management and IPC."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMainWindow

from my_idm.single_instance import SingleInstanceManager, activate_window

app = QApplication.instance() or QApplication([])

REPO_ROOT = Path(__file__).resolve().parent.parent

#: How long a child process may take to import PySide6 and bind its QLocalServer.
CHILD_STARTUP_TIMEOUT_S = 120.0
#: How long the primary child may take to deliver the message and exit.
CHILD_MESSAGE_TIMEOUT_S = 30.0


def terminate_process(proc: subprocess.Popen, grace: float = 10.0) -> None:
    """Kill and reap ``proc`` if it is still alive.

    Registered with ``addCleanup`` immediately after ``Popen`` so a failing
    assertion can never leave a Qt child process running. An orphan would keep
    the ``QLocalServer`` named pipe alive for the whole session, which makes every
    later test flaky in ways that look like product bugs.
    """
    if proc.poll() is not None:
        # Still reap: poll() already collected the exit status, but be explicit.
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
            pass


def wait_for_marker_file(
    marker_path: Path,
    expected: str,
    timeout: float,
    proc: subprocess.Popen,
    stderr_path: Path,
) -> str:
    """Poll ``marker_path`` until it contains ``expected`` or the deadline passes.

    A blocking ``stdout.readline()`` has no timeout: if the child dies before it
    prints (a missing PySide6, an import error), the suite hangs forever. Polling a
    marker file gives the handshake a hard deadline, and a failure can quote the
    child's own stderr instead of "expected READY, got ''".
    """
    deadline = time.monotonic() + timeout
    last_seen = ""
    while time.monotonic() < deadline:
        try:
            last_seen = marker_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            last_seen = ""
        except OSError:
            last_seen = ""
        if expected in last_seen:
            return last_seen
        if proc.poll() is not None and not last_seen:
            break
        time.sleep(0.05)

    stderr_text = ""
    try:
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr_text = ""
    raise AssertionError(
        f"child process never wrote {expected!r} to {marker_path} within {timeout}s.\n"
        f"  child exit code: {proc.poll()}\n"
        f"  last marker contents: {last_seen!r}\n"
        f"  child stderr:\n{stderr_text}"
    )


class TestSingleInstance(unittest.TestCase):

    def setUp(self):
        self.unique_server = f"test_idm_ipc_{uuid.uuid4().hex[:12]}"
        self.manager = SingleInstanceManager(self.unique_server)
        self.addCleanup(self.manager.close)
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="test_single_instance_"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)

    def tearDown(self):
        self.manager.close()

    def test_start_and_close_server(self):
        """Server starts listening and cleans up on close."""
        self.assertTrue(
            self.manager.start_server(),
            f"QLocalServer failed to listen on unique name {self.unique_server!r}; "
            "a stale server from a previous run would cause this",
        )
        self.assertIsNotNone(self.manager._server)
        self.assertTrue(self.manager._server.isListening())

        self.manager.close()
        self.assertIsNone(self.manager._server)

    def test_send_message_when_no_server_running_returns_false(self):
        """Sending a message when no primary instance exists returns False."""
        self.assertFalse(
            self.manager.send_message({"action": "activate"}),
            "send_message() must report False rather than block when no server listens",
        )

    def test_send_message_to_absent_server_is_time_bounded(self):
        """A missing server must not make send_message() block indefinitely."""
        started = time.monotonic()
        result = self.manager.send_message({"action": "activate"})
        elapsed = time.monotonic() - started
        self.assertFalse(result, "no server is listening on the unique test name")
        self.assertLess(
            elapsed, 10.0,
            f"send_message() against a dead server took {elapsed:.2f}s; the default "
            "1500 ms connect timeout must be honoured",
        )

    def test_send_message_honours_an_explicit_timeout(self):
        """An explicit timeout_ms must bound a send against a server that is not there."""
        started = time.monotonic()
        result = self.manager.send_message({"action": "activate"}, timeout_ms=250)
        elapsed = time.monotonic() - started
        self.assertFalse(result, "no server is listening on the unique test name")
        self.assertLess(
            elapsed, 5.0,
            f"send_message(timeout_ms=250) blocked for {elapsed:.2f}s; the caller "
            "supplied timeout must be honoured",
        )

    def test_activate_window_normal_and_minimized(self):
        """activate_window restores minimized windows and brings them to focus."""
        win = QMainWindow()
        # Registered *before* anything can fail, so a failing assert can never
        # leave a top-level widget alive for every later test in the session.
        self.addCleanup(win.close)
        self.addCleanup(win.deleteLater)
        win.setWindowTitle("Single Instance Test Window")
        win.resize(300, 200)
        win.show()
        app.processEvents()

        with mock.patch("ctypes.windll.user32.IsIconic", side_effect=lambda h: 0) as iconic, \
             mock.patch("ctypes.windll.user32.ShowWindow") as show_window, \
             mock.patch("ctypes.windll.user32.SetForegroundWindow") as set_fg:
            # Normal activate
            activate_window(win)
            app.processEvents()
            self.assertFalse(win.isMinimized(), "activate_window must not minimize a normal window")
            self.assertFalse(win.isHidden(), "activate_window must not hide a normal window")
            hwnd = int(win.winId())
            iconic.assert_called_once_with(hwnd)
            # SW_SHOW = 5 for a window Windows does not report as iconic
            show_window.assert_called_once_with(hwnd, 5)
            set_fg.assert_called_once_with(hwnd)

        # Second block: the Qt restoration already ran inside activate_window, so
        # IsIconic must be driven to 1 explicitly to model the native window that
        # Windows still reports as iconic immediately after showNormal().
        with mock.patch("ctypes.windll.user32.IsIconic", return_value=1) as iconic, \
             mock.patch("ctypes.windll.user32.ShowWindow") as show_window, \
             mock.patch("ctypes.windll.user32.SetForegroundWindow") as set_fg:
            # Minimized activate
            win.showMinimized()
            app.processEvents()
            self.assertTrue(win.isMinimized(), "showMinimized() did not minimize the window")

            activate_window(win)
            app.processEvents()
            self.assertFalse(
                win.isMinimized(),
                "activate_window() must restore a minimized window before focusing it",
            )
            self.assertEqual(
                win.windowState() & Qt.WindowState.WindowMinimized,
                Qt.WindowState.WindowNoState,
                "the minimized bit must be cleared from the window state",
            )
            hwnd = int(win.winId())
            iconic.assert_called_once_with(hwnd)
            # SW_RESTORE = 9 for a window Windows reports as iconic
            show_window.assert_called_once_with(hwnd, 9)
            set_fg.assert_called_once_with(hwnd)

        # Gracefully handle None
        with mock.patch("ctypes.windll.user32.SetForegroundWindow") as set_fg:
            activate_window(None)
            set_fg.assert_not_called()

        win.close()

    def test_activate_window_restores_minimized_state_without_showing_first(self):
        """A window that is minimized but never shown is still restored."""
        win = QMainWindow()
        self.addCleanup(win.close)
        self.addCleanup(win.deleteLater)
        win.setWindowState(Qt.WindowState.WindowMinimized)
        self.assertTrue(win.isMinimized())

        with mock.patch("ctypes.windll.user32.IsIconic", return_value=1) as iconic, \
             mock.patch("ctypes.windll.user32.ShowWindow") as show_window, \
             mock.patch("ctypes.windll.user32.SetForegroundWindow") as set_fg:
            activate_window(win)
            app.processEvents()

        self.assertFalse(win.isMinimized())
        hwnd = int(win.winId())
        iconic.assert_called_once_with(hwnd)
        show_window.assert_called_once_with(hwnd, 9)
        set_fg.assert_called_once_with(hwnd)
        win.hide()

    def test_activate_window_never_raises_from_the_win32_block(self):
        """A failing Win32 call is logged, not propagated to the IPC handler."""
        win = QMainWindow()
        self.addCleanup(win.close)
        self.addCleanup(win.deleteLater)

        with mock.patch("ctypes.windll.user32.IsIconic", side_effect=OSError("boom")), \
             mock.patch("ctypes.windll.user32.SetForegroundWindow", side_effect=OSError("boom")):
            # Must not raise: the caller is a Qt signal handler.
            activate_window(win)
        win.hide()

    def test_multi_process_ipc_payload_delivery(self):
        """A secondary process successfully transmits activation payload to primary process."""
        python_exe = sys.executable
        server_name = f"test_mp_ipc_{uuid.uuid4().hex[:12]}"
        ready_file = self.tmp_dir / "ready.txt"
        p1_stdout_path = self.tmp_dir / "p1_stdout.txt"
        p1_stderr_path = self.tmp_dir / "p1_stderr.txt"

        # Process 1 script. It ends in app.exec() (a live Qt event loop), so the
        # parent MUST bound the handshake and guarantee the child is reaped.
        p1_code = """
import json
import sys
from PySide6.QtWidgets import QApplication
from my_idm.single_instance import SingleInstanceManager

server_name, ready_path = sys.argv[1], sys.argv[2]

def mark(text):
    with open(ready_path, "a", encoding="utf-8") as fh:
        fh.write(text + "\\n")
        fh.flush()

app = QApplication([])
mgr = SingleInstanceManager(server_name)
if not mgr.start_server():
    mark("START_FAILED")
    sys.exit(1)

def on_msg(payload):
    print("MSG:" + json.dumps(payload), flush=True)
    mark("MESSAGE_DELIVERED")
    app.quit()

mgr.message_received.connect(on_msg)
mark("READY")
app.exec()
mgr.close()
"""

        # stdout/stderr go to real files, never to an undrained pipe: a pipe that
        # nobody reads deadlocks the child as soon as it passes the 64 KB buffer.
        with open(p1_stdout_path, "w", encoding="utf-8") as stdout_fh, \
             open(p1_stderr_path, "w", encoding="utf-8") as stderr_fh:
            p1 = subprocess.Popen(
                [python_exe, "-c", p1_code, server_name, str(ready_file)],
                stdout=stdout_fh,
                stderr=stderr_fh,
                text=True,
                cwd=str(REPO_ROOT),
            )
            # Registered immediately, before any assertion can fail, so the child
            # is always killed and reaped.
            self.addCleanup(terminate_process, p1)

            try:
                marker = wait_for_marker_file(
                    ready_file, "READY", CHILD_STARTUP_TIMEOUT_S, p1, p1_stderr_path
                )
                self.assertNotIn(
                    "START_FAILED",
                    marker,
                    f"primary child could not bind QLocalServer {server_name!r}",
                )

                # Process 2 script
                p2_code = f"""
import sys
from PySide6.QtWidgets import QApplication
from my_idm.single_instance import SingleInstanceManager

app = QApplication([])
mgr = SingleInstanceManager("{server_name}")
payload = {{"action": "activate", "backlog": "urls.txt", "urls": ["https://example.com/test.iso"]}}
success = mgr.send_message(payload)
print("SENT:" + str(success), flush=True)
"""

                p2 = subprocess.run(
                    [python_exe, "-c", p2_code],
                    capture_output=True,
                    text=True,
                    timeout=CHILD_MESSAGE_TIMEOUT_S,
                    cwd=str(REPO_ROOT),
                )

                self.assertEqual(
                    p2.returncode,
                    0,
                    f"secondary process exited {p2.returncode}\n"
                    f"  stdout: {p2.stdout!r}\n  stderr: {p2.stderr!r}",
                )
                self.assertIn(
                    "SENT:True",
                    p2.stdout,
                    f"secondary process failed to deliver the payload.\n"
                    f"  stdout: {p2.stdout!r}\n  stderr: {p2.stderr!r}",
                )

                wait_for_marker_file(
                    ready_file, "MESSAGE_DELIVERED", CHILD_MESSAGE_TIMEOUT_S,
                    p1, p1_stderr_path,
                )
                p1.wait(timeout=CHILD_MESSAGE_TIMEOUT_S)
                self.assertEqual(
                    p1.returncode,
                    0,
                    "primary child exited non-zero after quitting its event loop.\n"
                    f"  stderr: {p1_stderr_path.read_text(encoding='utf-8', errors='replace')!r}",
                )
            finally:
                # Belt and braces: addCleanup also runs, but killing here means the
                # QLocalServer name is released before the next test starts.
                terminate_process(p1)

        p1_stderr = p1_stderr_path.read_text(encoding="utf-8", errors="replace")
        p1_stdout = p1_stdout_path.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn(
            "Traceback",
            p1_stderr,
            f"primary child raised during the IPC handshake:\n{p1_stderr}",
        )

        msg_lines = [line for line in p1_stdout.splitlines() if line.startswith("MSG:")]
        self.assertEqual(
            len(msg_lines), 1,
            f"exactly one MSG line expected from the primary child, got {msg_lines!r}.\n"
            f"  stdout: {p1_stdout!r}\n  stderr: {p1_stderr!r}",
        )
        data = json.loads(msg_lines[0][4:])
        self.assertEqual(data["action"], "activate")
        self.assertEqual(data["backlog"], "urls.txt")
        self.assertEqual(data["urls"], ["https://example.com/test.iso"])

    def test_multi_process_send_fails_when_primary_absent(self):
        """A secondary process reports failure when no primary holds the pipe."""
        python_exe = sys.executable
        server_name = f"test_mp_absent_{uuid.uuid4().hex[:12]}"
        # Nothing is listening on this name: send_message() must return False
        # rather than hang or raise.
        p2_code = f"""
import sys
from PySide6.QtWidgets import QApplication
from my_idm.single_instance import SingleInstanceManager

app = QApplication([])
mgr = SingleInstanceManager("{server_name}")
print("SENT:" + str(mgr.send_message({{"action": "activate"}})), flush=True)
"""
        p2 = subprocess.run(
            [python_exe, "-c", p2_code],
            capture_output=True,
            text=True,
            timeout=CHILD_MESSAGE_TIMEOUT_S,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(
            p2.returncode, 0,
            f"secondary process exited {p2.returncode}\n  stderr: {p2.stderr!r}",
        )
        self.assertIn(
            "SENT:False",
            p2.stdout,
            f"send_message() with no primary must return False.\n"
            f"  stdout: {p2.stdout!r}\n  stderr: {p2.stderr!r}",
        )

    def test_stale_server_cleanup_and_restart(self):
        """start_server cleans up any previous pipe or socket file on restart."""
        server_name = f"test_stale_{uuid.uuid4().hex[:12]}"
        mgr1 = SingleInstanceManager(server_name)
        self.addCleanup(mgr1.close)
        self.assertTrue(mgr1.start_server())

        # Abandon mgr1 without close to simulate crash
        mgr2 = SingleInstanceManager(server_name)
        self.addCleanup(mgr2.close)
        # mgr2 start_server cleans up and succeeds
        self.assertTrue(mgr2.start_server())
        self.assertTrue(mgr2._server.isListening())
        mgr2.close()
        mgr1.close()


if __name__ == "__main__":
    unittest.main()
