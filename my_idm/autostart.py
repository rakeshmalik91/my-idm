"""Launch-at-login (auto-start on boot) registration, per platform.

My-IDM is launched by the operating system rather than by the user, so the registered entry has to
name a command that still exists. That is the whole difficulty of this feature and the reason it is
a module rather than three lines in the settings dialog:

* **The entry goes stale silently.** A user renames the checkout, moves the virtualenv, switches
  interpreters, or deletes the project. The OS keeps launching whatever path was registered, so
  every login produces a process that dies instantly with nothing on screen to explain it. So
  :func:`status` compares the *stored* command against the *current* one and reports
  :attr:`AutostartState.STALE` rather than answering "enabled" for an entry that can no longer run.
* **Every platform stores the command as an opaque string** (a registry value, an ``Exec=`` line, a
  plist array), so there is nothing to validate at registration time. Validation happens on read.
* **The three mechanisms are genuinely different** and none of them is emulatable by the others:
  ``HKCU\\...\\Run`` on Windows, an XDG ``.desktop`` file on Linux, a ``launchd`` agent on macOS.

Backends are selected once at import (:data:`_BACKEND`) so the platform question is answered in one
place. Every public function is a thin wrapper over the backend and never raises: registering a
launch item is a convenience, and a user who cannot get one must still be able to run My-IDM and
change every other preference.
"""

from __future__ import annotations

import enum
import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: Value name used in the Windows ``Run`` key, and the stem of the Linux ``.desktop`` file.
APP_ID = "My-IDM"

#: macOS ``launchd`` job label. Reverse-DNS, lower case: ``launchd`` treats the label as an
#: identifier and a spaced one is awkward to address with ``launchctl``.
MACOS_LABEL = "com.myidm.launcher"


class AutostartState(enum.Enum):
    """What the OS currently has registered for this app."""

    #: Nothing registered, or the registration was removed.
    DISABLED = "disabled"
    #: A registration exists and names the command this build would write.
    ENABLED = "enabled"
    #: A registration exists but names something else - a moved checkout, a different interpreter.
    #: Launching at login will fail, so this must be repairable rather than reported as working.
    STALE = "stale"
    #: The platform has no supported mechanism, or the check itself failed.
    UNSUPPORTED = "unsupported"


# ---------------------------------------------------------------------------------------------
# The command that relaunches this build
# ---------------------------------------------------------------------------------------------


def _pythonw_path() -> str:
    """The windowless sibling interpreter on Windows, else the running interpreter.

    Registering ``python.exe`` means a console window flashes on every login. ``external_tools``
    already has this lookup for its detached GUI launches; it is repeated rather than imported
    because that module pulls in ``ExternalToolsConfig`` and ``QtCore``, and autostart has to stay
    importable before any of the GUI exists.
    """
    if sys.platform != "win32":
        return sys.executable
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def app_command() -> list[str]:
    """The argv that starts this installation of My-IDM, with no behaviour flags.

    The shared base for every way the OS is asked to launch the app. :func:`launch_command` adds
    ``--autostart`` on top for the login item; a file-association entry does not, because
    ``--autostart`` suppresses the main window whenever a tray is available (``main.py``), so
    double-clicking a ``.torrent`` would add it invisibly with no feedback.

    Two shapes exist and they are not interchangeable:

    * **Frozen** (PyInstaller, py2app) - ``sys.executable`` *is* the app. Registering an
      interpreter here would launch the packaging tool's bootloader instead of My-IDM.
    * **Source checkout** - the interpreter plus ``-m my_idm.main``, run from the project root so
      the relative paths the app relies on (``backlog.txt``, ``run.bat``) resolve the way they do
      for a manual launch. ``pythonw`` is only correct on Windows, where it is the console-less
      interpreter; on POSIX the windowless case is handled by ``start_new_session`` at spawn time,
      which the desktop entry cannot express.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]

    return [_pythonw_path(), "-m", "my_idm.main"]


def launch_command() -> list[str]:
    """The argv that starts this installation of My-IDM again.

    :func:`app_command` plus ``--autostart``, which asks for a start with no window. See that
    function for why the frozen and source-checkout shapes differ.
    """
    return [*app_command(), "--autostart"]


def _command_cwd() -> Optional[str]:
    """Working directory for the registered command, or ``None`` for the user's home.

    A frozen build is launched by the OS from an unspecified directory and needs none; a source
    checkout needs the project root so ``-m my_idm.main`` resolves and ``backlog.txt`` in the repo
    is found. Stored separately from :func:`launch_command` because only some platforms can express
    it (Windows ``Run`` and XDG both cannot; ``launchd`` can).
    """
    if getattr(sys, "frozen", False):
        return None
    return str(Path(__file__).resolve().parent.parent)


def _command_string() -> str:
    """The registered command rendered as one string, for platforms that store a string.

    Windows ``Run`` values and XDG ``Exec=`` lines are both single strings, but they quote
    differently: Windows uses ``CommandLineToArgvW`` rules (``subprocess.list2cmdline``), while the
    desktop-entry spec has its own escaping and quoting that ``list2cmdline`` does not implement.
    The two renderers are therefore separate functions rather than one shared one.
    """
    return subprocess.list2cmdline(launch_command())


def _desktop_exec() -> str:
    """Render :func:`launch_command` as an XDG ``Exec=`` value.

    The desktop-entry spec reserves backslash, double quote, backtick and dollar inside a quoted
    argument, and treats everything between the outer double quotes as literal. Paths routinely
    contain at least one of those on the platforms this runs on (``C:\\Users\\...``), so the
    escaping is not theoretical.
    """
    parts = []
    for arg in launch_command():
        escaped = (
            arg.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
        )
        parts.append(f'"{escaped}"')
    return " ".join(parts)


# ---------------------------------------------------------------------------------------------
# Windows: HKCU\Software\Microsoft\Windows\CurrentVersion\Run
# ---------------------------------------------------------------------------------------------

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_read() -> Optional[str]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_ID)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # A missing key is FileNotFoundError; anything else is a real failure (locked hive,
        # policy). Neither should propagate into a settings dialog.
        log.debug("Could not read the Windows Run key: %s", exc)
        return None


def _win_write(command: Optional[str]) -> tuple[bool, str]:
    import winreg

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if command is None:
                try:
                    winreg.DeleteValue(key, APP_ID)
                except FileNotFoundError:
                    pass  # already gone; the requested end state is reached either way
            else:
                winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, command)
        return True, ""
    except OSError as exc:
        log.warning("Could not update the Windows Run key: %s", exc)
        return False, f"Could not update the Windows startup registry entry: {exc}"


# ---------------------------------------------------------------------------------------------
# Linux: XDG autostart .desktop file
# ---------------------------------------------------------------------------------------------


def _xdg_config_home() -> Path:
    """``$XDG_CONFIG_HOME``, defaulted per the XDG base-directory spec.

    An empty or relative value is ignored rather than honoured: the spec says a relative path is
    invalid, and joining one onto a home directory would silently write the entry somewhere no
    desktop environment looks.
    """
    raw = os.environ.get("XDG_CONFIG_HOME", "")
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path.home() / ".config"


def _linux_entry_path() -> Path:
    return _xdg_config_home() / "autostart" / f"my-idm.desktop"


def _linux_read() -> Optional[str]:
    """The ``Exec=`` value from our ``.desktop`` file, or ``None``.

    Parsed rather than returned whole: the file carries ``Name``, ``Type`` and friends that say
    nothing about whether the entry still works.
    """
    path = _linux_entry_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.startswith("Exec="):
            return line[len("Exec="):].strip()
    return None


_DESKTOP_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=My-IDM
Comment=My-IDM download manager
Exec={exec_line}
Icon=my-idm
Terminal=false
Categories=Network;FileTransfer;
X-GNOME-Autostart-enabled=true
"""


def _linux_write(command: Optional[str]) -> tuple[bool, str]:
    path = _linux_entry_path()
    try:
        if command is None:
            path.unlink(missing_ok=True)
            return True, ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DESKTOP_TEMPLATE.format(exec_line=_desktop_exec()), encoding="utf-8")
        return True, ""
    except OSError as exc:
        log.warning("Could not update the XDG autostart entry: %s", exc)
        return False, f"Could not write '{path}': {exc}"


# ---------------------------------------------------------------------------------------------
# macOS: a launchd user agent
# ---------------------------------------------------------------------------------------------


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"


def _macos_read() -> Optional[list]:
    try:
        with _macos_plist_path().open("rb") as handle:
            return plistlib.load(handle).get("ProgramArguments")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def _launchctl(*args: str) -> tuple[bool, str]:
    """Run ``launchctl`` and report failure as text rather than raising.

    The plist is the source of truth for whether autostart is enabled; ``launchctl`` only makes it
    take effect *now* rather than at the next login. So a missing or failing ``launchctl`` degrades
    to "takes effect after the next reboot", and is reported as such instead of being treated as a
    failure to register.
    """
    try:
        result = subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("launchctl %s unavailable: %s", " ".join(args), exc)
        return False, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        log.info("launchctl %s failed (%s): %s", " ".join(args), result.returncode, detail)
        return False, detail
    return True, ""


def _macos_target() -> str:
    """The ``launchd`` domain to address.

    ``gui/<uid>`` is the per-user GUI session. A plist in ``~/Library/LaunchAgents`` that is loaded
    into the *system* domain needs root and silently does nothing for a normal user, which is the
    usual cause of "I enabled it and nothing happened".
    """
    return f"gui/{os.getuid()}"


def _macos_write(command: Optional[list]) -> tuple[bool, str]:
    path = _macos_plist_path()
    try:
        if command is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict = {
                "Label": MACOS_LABEL,
                "ProgramArguments": command,
                "RunAtLoad": True,
                # Without this, launchd would restart My-IDM forever the moment the user quits it
                # from the tray - the opposite of what "start at login" means.
                "KeepAlive": False,
                "ProcessType": "Interactive",
                "LimitLoadToSessionType": "Aqua",
            }
            working_dir = _command_cwd()
            if working_dir:
                payload["WorkingDirectory"] = working_dir
            with path.open("wb") as handle:
                plistlib.dump(payload, handle)
    except OSError as exc:
        log.warning("Could not update the launchd agent: %s", exc)
        return False, f"Could not write '{path}': {exc}"

    return _load_now(command is not None)


def _load_now(enabled: bool) -> tuple[bool, str]:
    """Make the plist effective for the current session, not just the next login.

    Split out from :func:`_macos_write` because the plist is the source of truth for whether
    autostart is on: ``launchctl`` only re-reads it. Failing here is therefore a success with a
    caveat - the entry is written and will be honoured at the next login - and is reported as one
    rather than as a failed registration.

    Skipped entirely off ``darwin``. That is not just for tests: this module is imported on every
    platform, and shelling out to a command that cannot exist is pointless.
    """
    if sys.platform != "darwin":
        return True, ""

    domain = _macos_target()
    _launchctl("bootout", f"{domain}/{MACOS_LABEL}")
    if not enabled:
        return True, ""
    ok, detail = _launchctl("bootstrap", domain, str(_macos_plist_path()))
    if not ok:
        return True, (
            "Registered, but launchctl could not load it for this session "
            f"({detail or 'no detail'}). It will start at the next login."
        )
    return True, ""


# ---------------------------------------------------------------------------------------------
# Backend selection and public API
# ---------------------------------------------------------------------------------------------


class _Backend:
    """One platform's registration mechanism behind a uniform interface."""

    name = "unsupported"

    def location(self) -> str:
        raise NotImplementedError

    def read(self) -> Optional[object]:
        raise NotImplementedError

    def write(self, command: Optional[object]) -> tuple[bool, str]:
        raise NotImplementedError


class _WindowsBackend(_Backend):
    name = "windows"

    def location(self) -> str:
        return f"HKEY_CURRENT_USER\\{_WIN_RUN_KEY}\\{APP_ID}"

    def read(self) -> Optional[str]:
        return _win_read()

    def write(self, command: Optional[str]) -> tuple[bool, str]:
        return _win_write(command)


class _LinuxBackend(_Backend):
    name = "linux"

    def location(self) -> str:
        return str(_linux_entry_path())

    def read(self) -> Optional[str]:
        return _linux_read()

    def write(self, command: Optional[str]) -> tuple[bool, str]:
        return _linux_write(command)


class _MacOSBackend(_Backend):
    name = "macos"

    def location(self) -> str:
        return str(_macos_plist_path())

    def read(self) -> Optional[list]:
        return _macos_read()

    def write(self, command: Optional[list]) -> tuple[bool, str]:
        return _macos_write(command)


def _select_backend() -> _Backend:
    if sys.platform == "win32":
        return _WindowsBackend()
    if sys.platform == "darwin":
        return _MacOSBackend()
    if sys.platform.startswith("linux"):
        return _LinuxBackend()
    # FreeBSD and friends have no portable mechanism; say so rather than pretending.
    return _Backend()


_BACKEND = _select_backend()


def backend_name() -> str:
    """Which mechanism is in use, for display and for tests."""
    return _BACKEND.name


def location() -> str:
    """Human-readable description of where the registration lives."""
    if _BACKEND.name == "unsupported":
        return "not supported on this platform"
    return _BACKEND.location()


def status() -> AutostartState:
    """Compare what is registered against what this build would register.

    Never raises. A registration that exists but names a different command is :attr:`STALE`, not
    :attr:`ENABLED` - that distinction is the feature; without it the checkbox claims to be on
    while login silently launches a path that no longer exists.
    """
    if _BACKEND.name == "unsupported":
        return AutostartState.UNSUPPORTED
    try:
        stored = _BACKEND.read()
    except Exception as exc:  # defensive: a backend bug must not break the settings dialog
        log.warning("Autostart probe failed: %s", exc)
        return AutostartState.UNSUPPORTED
    if stored is None:
        return AutostartState.DISABLED
    return _classify(stored)


def _classify(stored: object) -> AutostartState:
    """Map a stored command onto an :class:`AutostartState`."""
    if _BACKEND.name == "windows":
        # Compare the rendered strings: the registry holds one string, so that is the only
        # comparable form. Normalised so a path-separator or quoting difference introduced by a
        # rebuild is not reported as a broken entry.
        return _compare(_normalise_windows(str(stored)), _normalise_windows(_command_string()))
    if _BACKEND.name == "linux":
        return _compare(_normalise_desktop(str(stored)), _normalise_desktop(_desktop_exec()))
    if _BACKEND.name == "macos":
        return _compare(list(stored or []), launch_command())
    return AutostartState.UNSUPPORTED


def _normalise_windows(command: str) -> str:
    return " ".join(command.replace("/", "\\").split()).lower()


def _normalise_desktop(exec_line: str) -> str:
    return " ".join(exec_line.replace("/", "\\").split()).lower()


def _compare(stored: object, expected: object) -> AutostartState:
    return AutostartState.ENABLED if stored == expected else AutostartState.STALE


def is_enabled() -> bool:
    """True only when a working registration exists.

    :attr:`STALE` is reported as ``False`` deliberately: the question a caller wants answered is
    "will launching at login actually start My-IDM", and for a stale entry the answer is no.
    :func:`status` is there for callers that need to tell the two apart.
    """
    return status() is AutostartState.ENABLED


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """Register or remove the launch-at-login entry.

    Returns ``(ok, message)``. ``message`` is non-empty only when something needs telling the
    user - most importantly when registration succeeded but could not be loaded for the current
    session, which is a success with a caveat, not a failure.
    """
    if _BACKEND.name == "unsupported":
        return False, (
            "My-IDM does not know how to register itself on this platform. "
            "Add it to your session's startup applications by hand."
        )

    # Idempotent, but only against the exact state requested. STALE is *not* "disabled" for this
    # purpose: turning it off must still remove the broken entry, and turning it on must rewrite it.
    current = status()
    if (enabled and current is AutostartState.ENABLED) or (
        not enabled and current is AutostartState.DISABLED
    ):
        return True, ""  # already correct; rewriting churns the file and resets its mtime

    try:
        # `_current_command` renders into the active backend's own representation - a single
        # string for Windows and XDG, a list for launchd - so the backends stay interchangeable.
        ok, message = _BACKEND.write(_current_command() if enabled else None)
    except Exception as exc:  # a backend bug must not abort saving unrelated preferences
        log.exception("Autostart write failed")
        return False, f"Could not update the startup registration: {exc}"

    if not ok:
        return False, message
    if enabled:
        log.info("Registered launch at login via %s: %s", _BACKEND.name, location())
    else:
        log.info("Removed launch at login via %s", _BACKEND.name)
    return True, message


def _current_command() -> object:
    """The command this build would register, in the active backend's representation."""
    if _BACKEND.name == "windows":
        return _command_string()
    if _BACKEND.name == "linux":
        return _desktop_exec()
    return launch_command()


def repair() -> tuple[bool, str]:
    """Rewrite the entry so it matches this build, without changing whether it is enabled.

    The repair path for :attr:`AutostartState.STALE` - a user who moved the checkout keeps the
    preference they set and gets a working entry back, rather than having to untick and retick a
    checkbox they can no longer trust.
    """
    if _BACKEND.name == "unsupported":
        return False, "Launch at login is not supported on this platform."
    try:
        ok, message = _BACKEND.write(_current_command())
    except Exception as exc:
        log.exception("Autostart repair failed")
        return False, f"Could not repair the startup registration: {exc}"
    return (True, message) if ok else (False, message)


def reconcile(preferred: bool) -> tuple[bool, str]:
    """Make the OS registration match a decision the user has just made.

    ``preferred`` is always the outcome of an explicit user action - a checkbox the user moved in
    the settings dialog - never a value read back at startup. That distinction is the caller's
    responsibility and it is where the protection lives:

    * The dialog calls this **only when the checkbox moved** (:attr:`GeneralConfig.launch_at_login`
      is compared against what was loaded). A Save that did not touch the box reaches no code
      path that writes, so an entry the user removed through Task Manager or their desktop's
      startup panel is never silently resurrected.
    * Reaching here at all therefore means "the user asked", and the registration is made to match,
      including when what is on disk is stale - a stale entry is broken rather than disabled, so
      rewriting it restores the launch they asked for.

    Turning it off always removes the entry, stale or not: there is no reading of "off" that
    leaves a broken autostart behind.
    """
    if status() is AutostartState.ENABLED:
        return (True, "") if preferred else set_enabled(False)
    return set_enabled(preferred)
