"""Unit tests for single instance application management and IPC."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from my_idm.single_instance import SingleInstanceManager, activate_window

app = QApplication.instance() or QApplication([])


class TestSingleInstance(unittest.TestCase):

    def setUp(self):
        self.unique_server = f"test_idm_ipc_{uuid.uuid4().hex[:12]}"
        self.manager = SingleInstanceManager(self.unique_server)

    def tearDown(self):
        self.manager.close()

    def test_start_and_close_server(self):
        """Server starts listening and cleans up on close."""
        self.assertTrue(self.manager.start_server())
        self.assertIsNotNone(self.manager._server)
        self.assertTrue(self.manager._server.isListening())

        self.manager.close()
        self.assertIsNone(self.manager._server)

    def test_send_message_when_no_server_running_returns_false(self):
        """Sending a message when no primary instance exists returns False."""
        self.assertFalse(self.manager.send_message({"action": "activate"}))

    def test_activate_window_normal_and_minimized(self):
        """activate_window restores minimized windows and brings them to focus."""
        win = QMainWindow()
        win.setWindowTitle("Single Instance Test Window")
        win.resize(300, 200)
        win.show()
        app.processEvents()

        # Normal activate
        activate_window(win)
        self.assertFalse(win.isMinimized())

        # Minimized activate
        win.showMinimized()
        app.processEvents()
        self.assertTrue(win.isMinimized())

        activate_window(win)
        app.processEvents()
        self.assertFalse(win.isMinimized())

        # Gracefully handle None
        activate_window(None)

        win.close()

    def test_multi_process_ipc_payload_delivery(self):
        """A secondary process successfully transmits activation payload to primary process."""
        python_exe = sys.executable
        server_name = f"test_mp_ipc_{uuid.uuid4().hex[:12]}"

        # Process 1 script
        p1_code = f"""
import sys
import json
from PySide6.QtWidgets import QApplication
from my_idm.single_instance import SingleInstanceManager

app = QApplication([])
mgr = SingleInstanceManager("{server_name}")
if not mgr.start_server():
    print("START_FAILED", flush=True)
    sys.exit(1)

def on_msg(payload):
    print("MSG:" + json.dumps(payload), flush=True)
    app.quit()

mgr.message_received.connect(on_msg)
print("READY", flush=True)
app.exec()
mgr.close()
"""

        p1 = subprocess.Popen(
            [python_exe, "-c", p1_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        ready_line = p1.stdout.readline().strip()
        self.assertEqual(ready_line, "READY")

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
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        self.assertEqual(p2.returncode, 0)
        self.assertIn("SENT:True", p2.stdout)

        p1_stdout, p1_stderr = p1.communicate(timeout=5)
        self.assertIn("MSG:", p1_stdout)
        msg_line = [l for l in p1_stdout.splitlines() if l.startswith("MSG:")][0]
        data = json.loads(msg_line[4:])
        self.assertEqual(data["action"], "activate")
        self.assertEqual(data["backlog"], "urls.txt")
        self.assertEqual(data["urls"], ["https://example.com/test.iso"])

    def test_stale_server_cleanup_and_restart(self):
        """start_server cleans up any previous pipe or socket file on restart."""
        server_name = f"test_stale_{uuid.uuid4().hex[:12]}"
        mgr1 = SingleInstanceManager(server_name)
        self.assertTrue(mgr1.start_server())

        # Abandon mgr1 without close to simulate crash
        mgr2 = SingleInstanceManager(server_name)
        # mgr2 start_server cleans up and succeeds
        self.assertTrue(mgr2.start_server())
        mgr2.close()
        mgr1.close()


if __name__ == "__main__":
    unittest.main()
