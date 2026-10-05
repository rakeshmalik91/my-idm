"""Register My-IDM as a handler for ``.torrent`` files, per platform.

Companion to :mod:`my_idm.autostart`, and deliberately built like it: one platform backend chosen
at import, a ``status()`` that compares what is registered against what *this* build would
register, and a public API that never raises. The reason the two are the same shape is that they
answer the same question — "will the operating system do the right thing when the user acts on a
file?" — and the reason that question needs its own module is that the answer is mostly no.

**The hard part of this feature is that an application cannot make itself the default handler.**
On Windows, the user's choice lives in
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts\\.torrent\\UserChoice``
whose value is a hash the user generates by clicking through Default Apps; a program that writes
that key directly has its change silently discarded. On macOS, the association is declared by the
bundle's ``Info.plist`` and read by LaunchServices, so a non-bundled Python application has
nowhere to put it, and rewriting a signed bundle would invalidate the signature. Only Linux hands
the decision to the application, via ``xdg-mime``.

So this module does the part that is permitted everywhere and reports the part that is not:

* It writes a ProgID (Windows) or a desktop entry (Linux) naming the command that opens a
  ``.torrent``, which is what puts My-IDM in the file's *Open with* list.
* It then tells the truth about whether that made My-IDM the default, rather than reporting a
  checkbox as on when double-clicking still opens another program. That is the whole reason for
  :class:`FileAssocState` having a :attr:`~FileAssocState.REGISTERED` state distinct from
  :attr:`~FileAssocState.DEFAULT`, and why the settings dialog offers a button that opens the
  Default Apps page instead of silently failing.

The command registered is ``autostart.app_command()`` plus the opened path — deliberately *not*
``autostart.launch_command()``, which carries ``--autostart`` and would therefore add the file with
the window suppressed. Single-instance forwarding already handles the case where My-IDM is
running: the second launch hands the path to the primary and exits.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from my_idm.autostart import app_command

log = logging.getLogger(__name__)

#: The file type this module claims. The MIME type is what Linux and the desktop entry speak;
#: the suffix is what Windows keys on. Both are needed, and they are not the same string.
TORRENT_SUFFIX = ".torrent"
TORRENT_MIME_TYPE = "application/x-bittorrent"

#: Windows ProgID. Versioned, as the convention requires: a future incompatible bump takes
#: ``.2`` and coexists with this one instead of silently taking over users' files.
WINDOWS_PROG_ID = "MyIDM.Torrent.1"

#: Human-readable type name shown in Explorer's "Open with" list.
WINDOWS_FRIENDLY_NAME = "My-IDM Torrent File"

#: Stem of the XDG desktop entry. Also the basename ``xdg-mime`` is told to make default.
LINUX_DESKTOP_NAME = "my-idm-torrent.desktop"

#: Timeouts for the ``xdg-*`` helpers. They are local filesystem tools and should be instant;
#: a desktop session with a broken D-Bus can make them block, and a hung watcher thread is worse
#: than a failed registration the user is told about.
XDG_TIMEOUT_S = 10


class FileAssocState(enum.Enum):
    """What the OS currently does when a user opens a ``.torrent`` file."""

    #: Nothing registered, or the registration was removed.
    DISABLED = "disabled"
    #: Registered under a name this build does not own, or pointing at a different command than
    #: this build would write. Opening may work, or may launch a path that no longer exists, so
    #: this is repairable rather than reported as working.
    STALE = "stale"
    #: Registered and current, and My-IDM appears in the file's *Open with* list — but the OS
    #: still hands the file to something else by default. The normal state on Windows.
    REGISTERED = "registered"
    #: Registered, current, *and* the operating system's default handler for the type.
    DEFAULT = "default"
    #: No supported mechanism on this platform, or the check itself failed.
    UNSUPPORTED = "unsupported"


# ---------------------------------------------------------------------------------------------
# The command that opens a file handed to us by the OS
# ---------------------------------------------------------------------------------------------


def _windows_command() -> str:
    """The ``shell\\open\\command`` string: the app plus the path Windows substitutes ``%1``.

    ``subprocess.list2cmdline`` rather than a hand-rolled join, because this is a
    ``CommandLineToArgvW`` string: a project path containing a space is the normal case, and an
    unquoted one would make Windows pass the interpreter and the file as separate arguments of
    the wrong program.
    """
    return subprocess.list2cmdline([*app_command(), "%1"])


def _windows_icon() -> str:
    return subprocess.list2cmdline([app_command()[0]]) + ",0"


def _desktop_exec() -> str:
    """Render the open command as an XDG ``Exec=`` value.

    The desktop-entry spec reserves backslash, double quote, backtick and dollar inside a quoted
    argument, so the escaping is not theoretical on a ``C:\\Users\\...`` path. ``%f`` is the
    spec's field code for "the file to open"; ``%1`` means the same to KDE and GNOME but is not
    in the spec.
    """
    parts = []
    for arg in [*app_command(), "%f"]:
        escaped = (
            arg.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
        )
        parts.append(f'"{escaped}"')
    return " ".join(parts)


# ---------------------------------------------------------------------------------------------
# Windows: HKCU\Software\Classes
# ---------------------------------------------------------------------------------------------

_WIN_CLASSES_KEY = r"Software\Classes"
_WIN_ASSOC_KEY = rf"{_WIN_CLASSES_KEY}\.{TORRENT_SUFFIX.lstrip('.')}"
_WIN_PROG_KEY = rf"{_WIN_CLASSES_KEY}\{WINDOWS_PROG_ID}"
_WIN_COMMAND_KEY = rf"{_WIN_PROG_KEY}\shell\open\command"
_WIN_USER_CHOICE_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
    rf"\.{TORRENT_SUFFIX.lstrip('.')}\UserChoice"
)


def _win_read() -> Optional[str]:
    """The ``shell\\open\\command`` value, or ``None`` when this ProgID is not registered.

    Read-only over the ProgID, never over ``.torrent``'s own default value: that value may name
    another application entirely, and answering "registered" from it would report someone else's
    registration as ours.
    """
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_COMMAND_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "")
            return str(value)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # A missing key is FileNotFoundError; anything else is a real failure (locked hive,
        # policy). Neither should propagate into a settings dialog.
        log.debug("Could not read the .torrent ProgID: %s", exc)
        return None


def _win_is_default() -> bool:
    """Whether Windows currently hands ``.torrent`` files to our ProgID.

    Reads ``UserChoice\\ProgId``, which is a plain string even though the *hash* beside it is what
    actually protects the key. A program cannot write that key, but it can read this, which is
    all that is needed to tell the user the truth.
    """
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_USER_CHOICE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "ProgId")
            return str(value) == WINDOWS_PROG_ID
    except FileNotFoundError:
        # No UserChoice at all: the type has never been opened, so nobody is the default yet.
        return False
    except OSError as exc:
        log.debug("Could not read the .torrent default handler: %s", exc)
        return False


def _win_delete_tree() -> None:
    import winreg

    try:
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, _WIN_COMMAND_KEY)
    except OSError:
        pass
    for sub in ("DefaultIcon", "shell\\open", "shell"):
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, rf"{_WIN_PROG_KEY}\{sub}")
        except OSError:
            pass
    try:
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, _WIN_PROG_KEY)
    except OSError:
        pass


def _win_write(command: Optional[str]) -> tuple[bool, str]:
    """Register or remove the ProgID. ``None`` means remove.

    Every write is under ``HKEY_CURRENT_USER\\Software\\Classes``, which needs no elevation and
    affects only this user — deliberately. The machine-wide ``HKEY_CLASSES_ROOT`` write would
    need admin rights and would change association for every user on the box.
    """
    import winreg

    try:
        if command is None:
            _win_delete_tree()
            # Only reset the extension's default if it is ours. Another application may have
            # become the default since we registered, and clearing that would silently take the
            # type away from it.
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, _WIN_ASSOC_KEY
                ) as key:
                    current, _ = winreg.QueryValueEx(key, "")
                    if str(current) == WINDOWS_PROG_ID:
                        winreg.DeleteValue(key, "")
            except FileNotFoundError:
                pass
            except OSError as exc:
                return False, f"Could not read the .torrent association: {exc}"
            return True, ""

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _WIN_ASSOC_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, WINDOWS_PROG_ID)
            # The "Open with" list, which is what actually becomes useful to the user: a ProgID
            # registered here shows up under "Choose another app" even while another program is
            # still the default.
            winreg.SetValueEx(
                key, "OpenWithProgids", 0, winreg.REG_SZ, f"{WINDOWS_PROG_ID}\0"
            )
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _WIN_PROG_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, WINDOWS_FRIENDLY_NAME)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, rf"{_WIN_PROG_KEY}\DefaultIcon", 0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, _windows_icon())
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _WIN_COMMAND_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
        return True, ""
    except OSError as exc:
        log.warning("Could not update the .torrent file association: %s", exc)
        return False, f"Could not update the .torrent file association: {exc}"


# ---------------------------------------------------------------------------------------------
# Linux: XDG desktop entry plus the mimeapps default
# ---------------------------------------------------------------------------------------------


def _xdg_config_home() -> Path:
    """``$XDG_CONFIG_HOME``, defaulted per the XDG base-directory spec.

    A relative value is ignored rather than honoured: the spec calls it invalid, and joining one
    onto a home directory would write the entry somewhere no desktop environment looks.
    """
    raw = os.environ.get("XDG_CONFIG_HOME", "")
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path.home() / ".config"


def _linux_entry_path() -> Path:
    return _xdg_config_home() / "applications" / LINUX_DESKTOP_NAME


def _linux_mimeapps_path() -> Path:
    return _xdg_config_home() / "mimeapps.list"


def _linux_desktop_entry() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={WINDOWS_FRIENDLY_NAME}\n"
        "Comment=Add BitTorrent downloads to My-IDM\n"
        f"Exec={_desktop_exec()}\n"
        "Icon=my-idm\n"
        "Terminal=false\n"
        "Categories=Network;FileTransfer;P2P;\n"
        f"MimeType={TORRENT_MIME_TYPE};\n"
        "NoDisplay=false\n"
    )


def _linux_read() -> Optional[str]:
    """The stored ``Exec=`` line, or ``None``.

    The line is parsed out rather than the file returned so that a desktop entry which happens to
    contain the expected string in, say, its ``Comment`` cannot masquerade as a working one.
    """
    path = _linux_entry_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        if line.strip().startswith("Exec="):
            return line.strip()[len("Exec="):].strip()
    return None


def _linux_is_default() -> bool:
    """Whether ``mimeapps.list`` names our entry as the default for the torrent MIME type.

    The ``[Default Applications]`` section is ``mimetype=entry;entry2;`` — an equals-separated
    key whose *value* is the semicolon-separated list. Splitting the line on semicolons would
    return the whole ``mimetype=entry`` string as if it were an entry name, and this would then
    report "not the default" on a system where it plainly is.
    """
    for mimetype, entries in _linux_default_entries():
        if mimetype == TORRENT_MIME_TYPE:
            return LINUX_DESKTOP_NAME in entries
    return False


def _linux_default_entries() -> list[tuple[str, list[str]]]:
    """The parsed ``[Default Applications]`` section as ``[(mimetype, [entry, ...]), ...]``."""
    path = _linux_mimeapps_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return []
    out: list[tuple[str, list[str]]] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped == "[Default Applications]"
            continue
        if not in_section or not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        mimetype, _, value = stripped.partition("=")
        out.append(
            (mimetype.strip(), [e.strip() for e in value.split(";") if e.strip()])
        )
    return out


def _xdg(*args: str) -> tuple[bool, str]:
    """Run ``xdg-mime`` with *args*. Returns ``(ok, message)``; never raises.

    A missing helper is the normal case on a minimal system and not worth a message box, so it
    comes back as a failure with an empty message and the caller supplies wording.
    """
    try:
        result = subprocess.run(
            ["xdg-mime", *args], capture_output=True, text=True, timeout=XDG_TIMEOUT_S
        )
    except FileNotFoundError:
        return False, ""
    except subprocess.TimeoutExpired:
        return False, "The desktop's mime handler did not respond."
    except Exception as exc:
        log.debug("xdg-mime %s failed: %s", " ".join(args), exc)
        return False, ""
    return (result.returncode == 0), ""


def _update_desktop_database() -> None:
    """Refresh the desktop entry cache. Best effort; a missing helper only costs an icon."""
    try:
        subprocess.run(
            ["update-desktop-database", str(_linux_entry_path().parent)],
            capture_output=True, text=True, timeout=XDG_TIMEOUT_S,
        )
    except Exception as exc:
        log.debug("update-desktop-database failed: %s", exc)


def _linux_write(command: Optional[str]) -> tuple[bool, str]:
    """Write or remove the desktop entry. ``None`` means remove.

    The ``xdg-mime default`` call is what Linux actually permits an application to do, so unlike
    Windows the default really is set here. Removing cannot un-set it, so the entry is stripped
    from ``mimeapps.list`` directly; a desktop environment that later finds a dangling name simply
    ignores it, which is the same thing every uninstalled application leaves behind.
    """
    path = _linux_entry_path()
    if command is None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return False, f"Could not remove the desktop entry: {exc}"
        _linux_forget_default()
        _update_desktop_database()
        return True, ""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_linux_desktop_entry(), encoding="utf-8")
    except OSError as exc:
        return False, f"Could not write the desktop entry: {exc}"
    _update_desktop_database()
    ok, message = _xdg("default", LINUX_DESKTOP_NAME, TORRENT_MIME_TYPE)
    if not ok and not message:
        message = (
            "My-IDM is now offered for .torrent files, but this desktop did not let it become "
            "the default. Set it in your file manager's 'Open With' settings."
        )
    return True, message


def _linux_forget_default() -> None:
    """Strip our entry from ``[Default Applications]``, leaving every other line alone.

    Rewritten as a whole file rather than edited in place so the result cannot be a file that is
    half one format and half the other. A rewrite that fails is logged and abandoned rather than
    retried: the desktop entry is already gone, so a dangling name is inert, and a truncated
    ``mimeapps.list`` would break every other type's association too.
    """
    path = _linux_mimeapps_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return

    out: list[str] = []
    in_section = False
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped == "[Default Applications]"
            out.append(line)
            continue
        if (
            in_section
            and stripped
            and not stripped.startswith("#")
            and "=" in stripped
        ):
            mimetype, _, value = stripped.partition("=")
            if mimetype.strip() == TORRENT_MIME_TYPE:
                entries = [e.strip() for e in value.split(";") if e.strip()]
                remaining = [e for e in entries if e != LINUX_DESKTOP_NAME]
                changed = True
                if not remaining:
                    continue  # we were the only entry: drop the line rather than leave it empty
                newline = "\r\n" if line.endswith("\r\n") else "\n"
                out.append(f"{TORRENT_MIME_TYPE}={';'.join(remaining)}{newline}")
                continue
        out.append(line)

    if not changed:
        return
    try:
        path.write_text("".join(out), encoding="utf-8")
    except OSError as exc:
        log.debug("Could not rewrite mimeapps.list: %s", exc)


# ---------------------------------------------------------------------------------------------
# Backend selection and public API
# ---------------------------------------------------------------------------------------------


class _Backend:
    """One platform's association mechanism behind a uniform interface.

    ``supported`` is separate from ``name`` because macOS is a real platform this module
    recognises and has no mechanism for: it must report :attr:`FileAssocState.UNSUPPORTED` and say
    so, not fall through to the "nothing registered" answer and offer to write an entry into a
    bundle ``Info.plist`` that a running application cannot reach.
    """

    name = "unsupported"
    supported = False

    def location(self) -> str:
        raise NotImplementedError

    def unsupported_message(self) -> str:
        """What to tell the user when there is nothing this platform can do."""
        return (
            "My-IDM does not know how to register itself for .torrent files on this platform. "
            "Choose My-IDM from your file manager's 'Open with' menu."
        )

    def read(self) -> Optional[object]:
        raise NotImplementedError

    def is_default(self) -> bool:
        return False

    def write(self, command: Optional[object]) -> tuple[bool, str]:
        raise NotImplementedError


class _WindowsBackend(_Backend):
    name = "windows"
    supported = True

    def location(self) -> str:
        return f"HKEY_CURRENT_USER\\{_WIN_COMMAND_KEY}"

    def read(self) -> Optional[str]:
        return _win_read()

    def is_default(self) -> bool:
        return _win_is_default()

    def write(self, command: Optional[str]) -> tuple[bool, str]:
        return _win_write(command)


class _LinuxBackend(_Backend):
    name = "linux"
    supported = True

    def location(self) -> str:
        return str(_linux_entry_path())

    def read(self) -> Optional[str]:
        return _linux_read()

    def is_default(self) -> bool:
        return _linux_is_default()

    def write(self, command: Optional[str]) -> tuple[bool, str]:
        return _linux_write(command)


class _MacOSBackend(_Backend):
    """macOS has no per-user mechanism an unbundled application can write.

    LaunchServices reads document types from the bundle's ``Info.plist`` and caches them, so the
    honest answer is that this is unsupported here rather than a registration that silently does
    nothing. A frozen ``.app`` bundle would need a ``CFBundleDocumentTypes`` entry baked in at
    build time, which is a packaging change and not a runtime one.
    """

    name = "macos"
    supported = False

    def location(self) -> str:
        return "the application bundle's Info.plist (CFBundleDocumentTypes)"

    def unsupported_message(self) -> str:
        return (
            "macOS decides which app opens a .torrent from the application bundle itself, "
            "which My-IDM cannot change at runtime. Pick My-IDM in Finder's Get Info > "
            "Open with, or set it as the default in System Settings."
        )

    def read(self) -> Optional[str]:
        return None

    def write(self, command: Optional[str]) -> tuple[bool, str]:
        return False, self.unsupported_message()


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
    """Human-readable description of where the registration lives.

    Asks the backend rather than branching on its name: macOS is recognised and has no mechanism,
    and it can still say *where* one would go ("the bundle's Info.plist"), which is more use to the
    user than the generic "not supported on this platform".
    """
    try:
        return _BACKEND.location()
    except NotImplementedError:
        return "not supported on this platform"


def status() -> FileAssocState:
    """What the OS actually does today with a ``.torrent`` file.

    Never raises. Distinguishes :attr:`REGISTERED` from :attr:`DEFAULT` because on Windows that is
    the normal, permanent state: the registration is ours and correct, and the default is not.
    Reporting that as "on" would be the same lie this module exists to avoid.
    """
    if not _BACKEND.supported:
        return FileAssocState.UNSUPPORTED
    try:
        stored = _BACKEND.read()
    except Exception as exc:  # defensive: a backend bug must not break the settings dialog
        log.warning("File association probe failed: %s", exc)
        return FileAssocState.UNSUPPORTED
    if stored is None:
        return FileAssocState.DISABLED

    expected = _expected_command()
    if expected is None or _normalise(str(stored)) != _normalise(expected):
        return FileAssocState.STALE
    try:
        if _BACKEND.is_default():
            return FileAssocState.DEFAULT
    except Exception as exc:
        log.debug("Could not read the current default handler: %s", exc)
    return FileAssocState.REGISTERED


def _expected_command() -> Optional[str]:
    if _BACKEND.name == "windows":
        return _windows_command()
    if _BACKEND.name == "linux":
        return _desktop_exec()
    return None


def _normalise(command: str) -> str:
    return " ".join(command.replace("/", "\\").split()).lower()


def is_enabled() -> bool:
    """True when a current registration exists — whether or not it is the OS default.

    :attr:`REGISTERED` answers ``True`` because the question is "is My-IDM registered to open
    ``.torrent`` files", and on Windows it is, even while another program is still the default.

    :attr:`STALE` answers ``False``, and that is load-bearing rather than pedantic:
    :func:`reconcile` short-circuits on ``is_enabled()``, so a broken entry left as ``True``
    would make "repair the registration" a no-op and the stale command would survive forever.
    :func:`status` is where the two are told apart for display.
    """
    return status() in (FileAssocState.REGISTERED, FileAssocState.DEFAULT)


def is_default() -> bool:
    """True only when double-clicking a ``.torrent`` will actually reach My-IDM."""
    return status() is FileAssocState.DEFAULT


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """Register or remove the association. Returns ``(ok, message)``.

    Idempotent against the exact requested state, mirroring
    :func:`my_idm.autostart.set_enabled`. :attr:`STALE` is *not* treated as disabled: turning the
    option off must still remove the broken entry, and turning it on must rewrite it.
    """
    if not _BACKEND.supported:
        return False, _BACKEND.unsupported_message()

    current = status()
    if enabled and current in (FileAssocState.REGISTERED, FileAssocState.DEFAULT):
        return True, ""
    if not enabled and current is FileAssocState.DISABLED:
        return True, ""

    try:
        ok, message = _BACKEND.write(_expected_command() if enabled else None)
    except Exception as exc:  # a backend bug must not abort saving unrelated preferences
        log.exception("File association write failed")
        return False, f"Could not update the .torrent file association: {exc}"

    if not ok:
        return False, message
    if enabled:
        log.info("Registered .torrent association via %s: %s", _BACKEND.name, location())
    else:
        log.info("Removed .torrent association via %s", _BACKEND.name)
    return True, message


def repair() -> tuple[bool, str]:
    """Rewrite the entry so it names this build's command again."""
    return set_enabled(True)


def reconcile(preferred: bool) -> tuple[bool, str]:
    """Make the OS registration match an explicit user preference.

    ``preferred`` must be the outcome of a user action, never a value read back at startup. That
    duty is the caller's: it is what stops a Save which did not touch the checkbox from
    resurrecting an association the user removed in the system settings. Reaching this function
    therefore means the user asked, and a :attr:`STALE` entry is rewritten rather than trusted.
    """
    if is_enabled():
        return (True, "") if preferred else set_enabled(False)
    return set_enabled(preferred)
