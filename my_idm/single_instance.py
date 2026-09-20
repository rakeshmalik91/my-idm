"""Single instance application management using Qt Local Socket IPC."""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

DEFAULT_SERVER_NAME = "my_idm_single_instance_ipc"


def activate_window(window: Optional[QWidget]):
    """Restore, raise, and bring window to the foreground with focus."""
    if not window:
        return

    # 1. Qt window restoration
    try:
        if window.isMinimized():
            window.setWindowState(
                (window.windowState() & ~Qt.WindowState.WindowMinimized)
                | Qt.WindowState.WindowActive
            )
            window.showNormal()
        else:
            window.show()

        window.raise_()
        window.activateWindow()
    except Exception as exc:
        log.debug("Qt activateWindow error: %s", exc)

    # 2. Windows-specific foreground activation
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = int(window.winId())
            # SW_RESTORE = 9, SW_SHOW = 5
            if ctypes.windll.user32.IsIconic(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, 9)
            else:
                ctypes.windll.user32.ShowWindow(hwnd, 5)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception as exc:
            log.debug("Windows SetForegroundWindow error: %s", exc)


class SingleInstanceManager(QObject):
    """Manages single-instance enforcement and IPC communication."""

    message_received = Signal(dict)

    def __init__(
        self,
        server_name: str = DEFAULT_SERVER_NAME,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.server_name = server_name
        self._server: Optional[QLocalServer] = None

    def send_message(self, payload: dict, timeout_ms: int = 1500) -> bool:
        """Send message payload to an existing running primary instance.
        
        Returns True if connected and message was sent, False otherwise.
        """
        if sys.platform == "win32":
            try:
                import ctypes
                # Allow target process to bring its window to the foreground
                ctypes.windll.user32.AllowSetForegroundWindow(-1)
            except Exception:
                pass

        socket = QLocalSocket()
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(timeout_ms):
            return False

        try:
            data = json.dumps(payload).encode("utf-8")
            socket.write(data)
            socket.waitForBytesWritten(timeout_ms)
            socket.flush()
            # Wait for server acknowledgment or clean disconnect
            socket.waitForReadyRead(timeout_ms)
            return True
        except Exception as exc:
            log.warning("Failed to send IPC message to primary instance: %s", exc)
            return False
        finally:
            socket.disconnectFromServer()

    def start_server(self) -> bool:
        """Start local IPC server. Removes any stale server instance."""
        QLocalServer.removeServer(self.server_name)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        success = self._server.listen(self.server_name)
        if success:
            log.debug("Single instance IPC server listening on %s", self.server_name)
        else:
            log.warning(
                "Failed to listen on single instance IPC server %s: %s",
                self.server_name,
                self._server.errorString(),
            )
        return success

    def _on_new_connection(self):
        if not self._server:
            return
        client = self._server.nextPendingConnection()
        if not client:
            return

        buffer = bytearray()

        def _process_data():
            nonlocal buffer
            try:
                if not client.isValid():
                    return
                data = client.readAll().data()
                if data:
                    buffer.extend(data)
                if buffer:
                    payload = json.loads(buffer.decode("utf-8"))
                    self.message_received.emit(payload)
                    buffer.clear()
                    try:
                        client.write(b"OK\n")
                        client.flush()
                    except Exception:
                        pass
            except Exception as exc:
                log.debug("IPC client read/process error: %s", exc)

        client.readyRead.connect(_process_data)
        client.disconnected.connect(client.deleteLater)

        if client.bytesAvailable() > 0:
            _process_data()

    def close(self):
        """Shutdown IPC server and clean up socket name."""
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            QLocalServer.removeServer(self.server_name)
            self._server = None
