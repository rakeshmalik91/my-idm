"""GUI launcher for My-IDM.

Runs My-IDM in windowed mode without opening a command prompt / console on Windows.
When double-clicked in Windows Explorer, Windows automatically executes this file
using pythonw.exe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repository root directory is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _maybe_reexec_in_venv():
    """If a local virtualenv exists and we're not running in it, re-exec with venv pythonw."""
    for venv_dir in (".venv", "venv"):
        venv_pythonw = PROJECT_ROOT / venv_dir / "Scripts" / "pythonw.exe"
        if venv_pythonw.is_file():
            current_exe = Path(sys.executable).resolve()
            if current_exe != venv_pythonw.resolve():
                import subprocess

                cmd = [str(venv_pythonw), str(Path(__file__).resolve())] + sys.argv[1:]
                subprocess.Popen(cmd)
                sys.exit(0)


def main():
    _maybe_reexec_in_venv()

    try:
        from my_idm.main import main as app_main

        sys.exit(app_main() or 0)
    except Exception:
        import traceback

        err_msg = traceback.format_exc()
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                f"An error occurred while launching My-IDM:\n\n{err_msg}",
                "My-IDM Startup Error",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
