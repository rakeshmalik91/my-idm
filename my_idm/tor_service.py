"""Tor background service process management and discovery."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from my_idm.config import TorConfig, is_tor_reachable

log = logging.getLogger(__name__)


def find_tor_executable(custom_path: str = "") -> Optional[str]:
    """Locate the Tor executable on the local system.

    Checks:
    1. The user-configured path if provided and exists.
    2. PATH environment variable ('tor', 'tor.exe').
    3. Common Windows installation locations (Tor Browser, Standalone Tor).
    4. Common Unix / macOS locations.
    """
    if custom_path and os.path.isfile(custom_path):
        return str(Path(custom_path).resolve())

    which_tor = shutil.which("tor") or shutil.which("tor.exe")
    if which_tor and os.path.isfile(which_tor):
        return str(Path(which_tor).resolve())

    # Standard Windows paths
    candidates: list[Path] = []
    if os.name == "nt":
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        app_data = os.environ.get("APPDATA", "")
        user_profile = os.environ.get("USERPROFILE", "")

        candidates.extend([
            Path(prog_files) / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
            Path(prog_files_x86) / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
            Path(prog_files) / "Tor" / "tor.exe",
            Path(prog_files_x86) / "Tor" / "tor.exe",
        ])
        if local_app_data:
            candidates.append(
                Path(local_app_data) / "Programs" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe"
            )
        if user_profile:
            candidates.append(
                Path(user_profile) / "Desktop" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe"
            )
        if app_data:
            candidates.append(Path(app_data) / "tor" / "tor.exe")
    else:
        # Linux & macOS common locations
        candidates.extend([
            Path("/usr/bin/tor"),
            Path("/usr/local/bin/tor"),
            Path("/opt/homebrew/bin/tor"),
            Path("/opt/local/bin/tor"),
        ])

    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except Exception:
            continue

    return None


class TorServiceManager:
    """Manages spawning, monitoring, and shutting down the Tor background process."""

    def __init__(self, config: TorConfig, data_dir: Optional[Path] = None):
        self._config = config
        self._data_dir = data_dir or (Path.home() / ".my-idm" / "tor_data")
        self._process: Optional[subprocess.Popen] = None
        self._spawned_by_us = False

    @property
    def is_spawned(self) -> bool:
        """True if the Tor process was launched by this application."""
        return self._spawned_by_us and self._process is not None and self._process.poll() is None

    def is_running(self) -> bool:
        """Check if Tor SOCKS5 proxy is responsive."""
        return is_tor_reachable(self._config.proxy_host, self._config.proxy_port, timeout=1.0)

    def start(self, timeout: float = 15.0) -> tuple[bool, str]:
        """Start the Tor service if not already running and wait for connection.

        Returns (success: bool, message: str).
        """
        host = self._config.proxy_host or "127.0.0.1"
        port = int(self._config.proxy_port or 9050)

        # 1. If already reachable, use the existing running service
        if is_tor_reachable(host, port, timeout=1.0):
            log.info("Tor proxy is already running and reachable at %s:%d", host, port)
            return True, f"Connected to existing Tor service at {host}:{port}"

        # 2. Locate Tor binary
        tor_exe = find_tor_executable(self._config.tor_executable_path)
        if not tor_exe:
            alt_port = 9150 if port == 9050 else 9050
            alt_desc = "Tor Browser" if alt_port == 9150 else "Tor Service"
            alt_hint = ""
            if is_tor_reachable(host, alt_port, timeout=0.5):
                alt_hint = (
                    f"\n\n💡 Note: {alt_desc} was detected actively running on port {alt_port}!\n"
                    f"You can switch the port to {alt_port} in Tools → Preferences → Tor Network to connect to it directly."
                )

            err = (
                f"Tor executable ('tor.exe') could not be found.\n\n"
                f"Searched in:\n"
                f"• Configured path: {self._config.tor_executable_path or '(none)'}\n"
                f"• System PATH\n"
                f"• Standard Tor Browser and Tor install locations\n\n"
                f"Please ensure Tor or Tor Browser is installed, or specify the full path to tor.exe in Preferences.{alt_hint}"
            )
            log.warning("Tor start failed: %s", err)
            return False, err

        # 3. Prepare data directory
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            err = f"Failed to create Tor data directory '{self._data_dir}': {exc}"
            log.error(err)
            return False, err

        # 4. Build command line
        cmd = [
            tor_exe,
            "--SocksPort", str(port),
            "--DataDirectory", str(self._data_dir),
        ]

        # Windows: hide console window
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        log.info("Spawning Tor background process: %s", " ".join(cmd))
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags,
            )
            self._spawned_by_us = True
            try:
                pid_file = self._data_dir / "tor.pid"
                pid_file.write_text(str(self._process.pid), encoding="utf-8")
            except Exception:
                pass
        except Exception as exc:
            self._process = None
            self._spawned_by_us = False
            err = f"Failed to execute Tor binary '{tor_exe}': {exc}"
            log.error(err)
            return False, err

        # 5. Wait for Tor SOCKS5 proxy to become responsive
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            # Check if process exited unexpectedly
            ret = self._process.poll()
            if ret is not None:
                stdout_data = ""
                stderr_data = ""
                try:
                    stdout_data, stderr_data = self._process.communicate(timeout=1.0)
                except Exception:
                    pass
                err_detail = stderr_data.strip() or stdout_data.strip() or f"Process exited with code {ret}"
                if isinstance(err_detail, bytes):
                    err_detail = err_detail.decode("utf-8", errors="replace")
                self._spawned_by_us = False
                self._process = None
                if "already in use" in err_detail.lower() or "could not bind" in err_detail.lower():
                    alt_port = 9150 if port == 9050 else 9050
                    alt_desc = "Tor Browser" if alt_port == 9150 else "Tor Service"
                    alt_note = ""
                    if is_tor_reachable(host, alt_port, timeout=0.5):
                        alt_note = f"\n• Alternatively, {alt_desc} is active on port {alt_port} — you can select port {alt_port} in Preferences."
                    err = (
                        f"Port {port} is already in use by another application or Tor instance.\n\n"
                        f"To resolve this conflict:\n"
                        f"• Change the port in Preferences → Tor Network (e.g. port 9150 for Tor Browser){alt_note}\n"
                        f"• Or close the conflicting application using port {port}."
                    )
                elif "data directory" in err_detail.lower() and "already using" in err_detail.lower():
                    err = (
                        f"Another Tor process is already using the data directory '{self._data_dir}'.\n\n"
                        f"Please ensure no orphan Tor background processes are running."
                    )
                else:
                    err = f"Tor process terminated unexpectedly (exit code {ret}):\n{err_detail}"
                log.error(err)
                return False, err

            if is_tor_reachable(host, port, timeout=0.5):
                log.info("Tor background service started successfully on %s:%d (PID %d)", host, port, self._process.pid)
                return True, f"Tor background service started and connected on {host}:{port}"

            time.sleep(0.5)

        # 6. Timeout waiting for connection
        self.stop()
        err = (
            f"Tor process started (PID {self._process.pid if self._process else 'unknown'}), "
            f"but connection to {host}:{port} timed out after {timeout} seconds.\n\n"
            f"Please verify that port {port} is not blocked by antivirus or firewall software."
        )
        log.error(err)
        return False, err

    def stop(self):
        """Terminate the background Tor process forcefully and cleanly."""
        # If we did not spawn Tor (e.g. connected to existing Tor Browser or system Tor), do not kill external process
        if not self._spawned_by_us and (self._process is None or self._process.poll() is not None):
            log.info("Tor was not spawned by My-IDM; leaving external service running untouched")
            return

        pid_file = self._data_dir / "tor.pid"
        target_pids: list[int] = []

        if self._process and self._process.poll() is None:
            target_pids.append(self._process.pid)

        if self._spawned_by_us and pid_file.exists():
            try:
                saved_pid = int(pid_file.read_text(encoding="utf-8").strip())
                if saved_pid not in target_pids:
                    target_pids.append(saved_pid)
            except Exception:
                pass

        if self._spawned_by_us and self._process and self._process.poll() is None:
            try:
                self._process.terminate()
            except Exception:
                pass

        for pid in target_pids:
            log.info("Terminating Tor background process (PID %d)", pid)
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    os.kill(pid, 15)  # SIGTERM
            except Exception as exc:
                log.debug("Error killing Tor process PID %d: %s", pid, exc)

        if self._process:
            try:
                if self._process.poll() is None:
                    self._process.kill()
                    self._process.wait(timeout=0.5)
            except Exception:
                pass

        try:
            if pid_file.exists():
                pid_file.unlink(missing_ok=True)
        except Exception:
            pass

        self._process = None
        self._spawned_by_us = False
