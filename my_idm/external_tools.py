"""External tools integration layer (AnimePahe scraper, CLI execution, GUI launchers)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from my_idm.config import ExternalToolsConfig
from my_idm.proc import background_kwargs

log = logging.getLogger(__name__)


def find_pythonw_executable() -> str:
    """Find pythonw.exe corresponding to current Python environment, or fallback to python."""
    py_dir = Path(sys.executable).parent
    pythonw = py_dir / "pythonw.exe"
    if pythonw.is_file():
        return str(pythonw)
    return sys.executable


def get_child_pids(parent_pid: int) -> set[int]:
    """Recursively retrieves all child process PIDs spawned by parent_pid on Windows."""
    child_pids: set[int] = set()
    if sys.platform != "win32":
        return child_pids
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        hSnapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if hSnapshot == -1:
            return child_pids
        pe32 = PROCESSENTRY32W()
        pe32.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(hSnapshot, ctypes.byref(pe32)):
            while True:
                if pe32.th32ParentProcessID == parent_pid:
                    child_pids.add(pe32.th32ProcessID)
                    child_pids.update(get_child_pids(pe32.th32ProcessID))
                if not kernel32.Process32NextW(hSnapshot, ctypes.byref(pe32)):
                    break
        kernel32.CloseHandle(hSnapshot)
    except Exception as exc:
        log.debug("Error enumerating child PIDs: %s", exc)
    return child_pids


def embedded_browser_supported() -> bool:
    """Whether the in-panel embedded browser view can work on this platform.

    Windows-only, and not for a fixable reason. Docking the browser means reparenting a foreign
    top-level window into a Qt widget: on X11 that needs ``XReparentWindow`` against a client
    window plus a matching event loop, and macOS has no comparable public API at all. Neither is
    a matter of finding the right handle - ``find_chrome_hwnd`` returns ``None`` off Windows for
    the same reason.

    The AnimePahe scraper itself is unaffected; only the in-panel view of it is unavailable.
    """
    return sys.platform == "win32"


def find_chrome_hwnd(parent_pid: Optional[int] = None) -> Optional[int]:
    """Find newly created top-level Chrome_WidgetWin_1 browser window HWND belonging to parent_pid tree."""
    if not embedded_browser_supported():
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32

        child_pids: set[int] = set()
        if parent_pid is not None:
            child_pids = get_child_pids(parent_pid)
            child_pids.add(parent_pid)

        found_hwnd = None
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def enum_cb(hwnd, lparam):
            nonlocal found_hwnd
            cbuf = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, cbuf, 512)
            if cbuf.value == "Chrome_WidgetWin_1":
                win_pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                if not child_pids or win_pid.value in child_pids:
                    rect = (ctypes.c_long * 4)()
                    user32.GetWindowRect(hwnd, rect)
                    w = rect[2] - rect[0]
                    h = rect[3] - rect[1]
                    # Skip tiny windows (tooltips, splitters) that share the class name.
                    # The floor is inclusive: 200x150 is the documented minimum, so a
                    # window of exactly that size qualifies.
                    if w >= 200 and h >= 150:
                        found_hwnd = hwnd
                        return False
            return True

        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        return found_hwnd
    except Exception as exc:
        log.debug("Error finding Chrome HWND: %s", exc)
        return None


def launch_animepahe_cli(
    config: ExternalToolsConfig,
    my_idm_dir: Optional[str] = None,
    container_hwnd: Optional[int] = None,
    url: Optional[str] = None,
    episodes: Optional[str] = None,
    quality: Optional[str] = None,
    lang: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> Tuple[bool, str, Optional[subprocess.Popen]]:
    """
    Launch AnimePahe scraper in background CLI mode.
    Outputs stdout and stderr into console_log.txt and forwards items to My-IDM backlog.
    Optionally targets a specific anime URL and episode range.
    """
    repo = config.get_effective_repo_path()
    if not repo or not os.path.isdir(repo):
        msg = f"AnimePahe repository directory does not exist: '{config.animepahe_repo_path}'"
        log.warning(msg)
        return False, msg, None

    script = Path(repo) / "animepahe_download.py"
    if not script.is_file():
        msg = f"animepahe_download.py not found in {repo}"
        log.warning(msg)
        return False, msg, None

    cmd = [sys.executable, "-u", str(script), "--my-idm"]
    if my_idm_dir:
        cmd.extend(["--my-idm-dir", str(my_idm_dir)])
    if url:
        cmd.extend(["--url", url.strip()])
    if episodes:
        clean_ep = re.sub(r"\s+", "", episodes.strip())
        cmd.extend(["-ep", clean_ep])
    if quality and quality.lower() != "auto":
        cmd.extend(["-q", quality.strip()])
    if lang and lang.lower() != "auto":
        l_flag = "en" if "dub" in lang.lower() or lang.lower() == "en" else "jap"
        cmd.extend(["-l", l_flag])
    cmd.append("-y")  # Skip interactive prompts in background CLI
    if extra_args:
        cmd.extend(extra_args)

    console_log = config.get_console_log_path()
    console_log.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(console_log, "a", encoding="utf-8") as log_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_file.write(f"\n=======================================================\n")
            log_file.write(f"  AnimePahe CLI Scraper Session Started: {timestamp}\n")
            log_file.write(f"  Command: {' '.join(cmd)}\n")
            log_file.write(f"=======================================================\n\n")
            log_file.flush()

            flags = 0
            if sys.platform == "win32":
                # CREATE_NO_WINDOW prevents command prompt window from appearing
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            if container_hwnd:
                env["ANIMEPAHE_EMBED_CONTAINER_HWND"] = str(container_hwnd)

            proc = subprocess.Popen(
                cmd,
                cwd=repo,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                **background_kwargs(),
            )
        msg = f"Started AnimePahe scraper in CLI mode (PID: {proc.pid})"
        log.info(msg)
        return True, msg, proc

    except Exception as exc:
        msg = f"Failed to launch AnimePahe scraper CLI: {exc}"
        log.error(msg)
        return False, msg, None


def launch_animepahe_gui(config: ExternalToolsConfig) -> Tuple[bool, str]:
    """
    Launch AnimePahe desktop GUI detached from My-IDM process.
    """
    repo = config.get_effective_repo_path()
    if not repo or not os.path.isdir(repo):
        msg = f"AnimePahe repository directory does not exist: '{config.animepahe_repo_path}'"
        log.warning(msg)
        return False, msg

    run_pyw = Path(repo) / "run.pyw"
    gui_py = Path(repo) / "gui.py"
    run_bat = Path(repo) / "run_gui.bat"

    pythonw = find_pythonw_executable()

    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    # Detached on every platform. On Windows the flags above already do it; on POSIX they are
    # ignored and `start_new_session` is what stops this GUI dying with the terminal My-IDM was
    # launched from. See my_idm.proc.background_kwargs.
    spawn_kwargs = background_kwargs(new_process_group=True)

    try:
        if run_pyw.is_file():
            cmd = [pythonw, str(run_pyw)]
            subprocess.Popen(
                cmd,
                cwd=repo,
                **spawn_kwargs,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            msg = "Launched AnimePahe GUI via run.pyw"
            log.info(msg)
            return True, msg

        elif gui_py.is_file():
            cmd = [pythonw, str(gui_py)]
            subprocess.Popen(
                cmd,
                cwd=repo,
                **spawn_kwargs,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            msg = "Launched AnimePahe GUI via gui.py"
            log.info(msg)
            return True, msg

        elif run_bat.is_file():
            cmd = f'cmd.exe /c start "" /D "{repo}" "{run_bat}"'
            subprocess.Popen(cmd, shell=True, cwd=repo)
            msg = "Launched AnimePahe GUI via run_gui.bat"
            log.info(msg)
            return True, msg

        else:
            # Fallback to animepahe_download.py --gui
            script = Path(repo) / "animepahe_download.py"
            if script.is_file():
                cmd = [pythonw, str(script), "--gui"]
                subprocess.Popen(cmd, cwd=repo, **spawn_kwargs, stdin=subprocess.DEVNULL)
                msg = "Launched AnimePahe GUI via animepahe_download.py --gui"
                log.info(msg)
                return True, msg

            return False, f"No valid GUI entry point found in {repo} (expected run.pyw, gui.py, or run_gui.bat)"

    except Exception as exc:
        msg = f"Failed to launch AnimePahe GUI: {exc}"
        log.error(msg)
        return False, msg


def open_file_in_default_app(file_path: Path | str, create_if_missing: bool = True) -> Tuple[bool, str]:
    """
    Open a file or directory with the system's default application or File Explorer.
    Creates an empty placeholder if it's a non-existent file and create_if_missing is True.
    """
    p = Path(file_path).resolve()

    # 1. Directory handling
    if p.is_dir():
        try:
            if sys.platform == "win32":
                os.startfile(str(p))
                return True, f"Opened folder '{p.name}'"
            url = QUrl.fromLocalFile(str(p))
            if QDesktopServices.openUrl(url):
                return True, f"Opened folder '{p.name}'"
            return False, f"Failed to open folder '{p.name}'"
        except Exception as exc:
            return False, f"Failed to open folder '{p}': {exc}"

    # 2. Non-existent file handling
    if not p.is_file():
        if create_if_missing:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(f"# Log file created on {datetime.now().isoformat()}\n", encoding="utf-8")
            except Exception as exc:
                return False, f"Could not create file '{p}': {exc}"
        else:
            return False, f"File does not exist: '{p}'"

    # 3. Existing file handling
    try:
        url = QUrl.fromLocalFile(str(p))
        success = QDesktopServices.openUrl(url)
        if success:
            return True, f"Opened '{p.name}'"
        # Fallback for Windows if openUrl fails
        if sys.platform == "win32":
            os.startfile(str(p))
            return True, f"Opened '{p.name}'"
        return False, f"Failed to open '{p.name}' with default application."
    except Exception as exc:
        return False, f"Failed to open '{p}': {exc}"


def show_in_folder(path: Path | str) -> Tuple[bool, str]:
    """
    Open File Explorer. If path is a file, highlights/selects the file.
    If path is a folder, opens the folder.
    """
    p = Path(path).resolve()
    if not p.exists():
        if p.parent.is_dir():
            p = p.parent
        else:
            return False, f"Path does not exist: '{p}'"

    try:
        if sys.platform == "win32":
            if p.is_file():
                subprocess.Popen(["explorer", f"/select,{p}"])
            else:
                os.startfile(str(p))
            return True, f"Opened '{p.name}' in File Explorer"

        target = p.parent if p.is_file() else p
        url = QUrl.fromLocalFile(str(target))
        if QDesktopServices.openUrl(url):
            return True, f"Opened '{p.name}'"
        return False, f"Failed to open '{p.name}'"
    except Exception as exc:
        return False, f"Failed to open '{p}': {exc}"

