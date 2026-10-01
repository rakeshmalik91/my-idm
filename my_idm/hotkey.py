"""System-wide hotkey registration for My-IDM.

Windows offers ``user32.RegisterHotKey``, which binds a chord to a process for as long as the
registration lives and then posts ``WM_HOTKEY`` to that process' message queue. Qt never
surfaces ``WM_HOTKEY`` as an event of its own, so the message has to be picked out of the
native event stream by a ``QAbstractNativeEventFilter`` and re-emitted as a Qt signal — which
is the entire reason this module exists instead of a ``QShortcut``.

Everything is raw ``ctypes`` behind ``sys.platform == "win32"``, matching how the rest of the
app reaches Windows APIs (``main.py``, ``single_instance.py``, ``details_panel.py``). No
``pywin32``: it is not in ``requirements.txt``, and a hotkey is not worth a dependency.
"""

from __future__ import annotations

import ctypes
import logging
import re
import sys
from typing import Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

log = logging.getLogger("my_idm")

#: Process-private hotkey id. Arbitrary, but it must not collide with an id anything else in
#: this process might register — nothing else here does.
HOTKEY_ID = 0xB101

WM_HOTKEY = 0x0312

#: ``RegisterHotKey`` failure meaning another application already owns this chord.
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

#: Modifier spellings accepted in a stored sequence, lower-cased. ``Meta`` is what Qt emits for
#: the Windows key; ``Win``/``Windows``/``Super`` are what a human types.
_MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "option": MOD_ALT,
    "shift": MOD_SHIFT,
    "meta": MOD_WIN,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "super": MOD_WIN,
}

#: At least one of these must be present. A bare letter or F-key would swallow that key in
#: every other application on the desktop, so such a chord is refused rather than registered —
#: a global hotkey that breaks typing in the user's editor is worse than no hotkey at all.
_REQUIRED_MODIFIERS = (MOD_ALT, MOD_CONTROL, MOD_WIN)

#: Splits a stored sequence into its leading modifier run and the key itself. Written against
#: the *string*, not ``QKeySequence``: Qt drops a Meta/Win chord entirely
#: (``QKeySequence("Win+D")`` is empty), so a Qt-based parser cannot express the Windows key at
#: all — and Qt encodes F-keys and arrows in a private 0x01xxxxxx range that does not line up
#: with Win32 virtual-key codes. The string is what the user typed and what
#: ``QKeySequenceEdit`` hands back, so it is the honest input.
_SEQUENCE_RE = re.compile(
    r"^(?P<mods>(?:(?:ctrl|control|alt|option|shift|meta|win|windows|super)\+)+)"
    r"(?P<key>.+)$",
    re.IGNORECASE,
)

_F_KEY_RE = re.compile(r"^F([1-9]|1[0-9]|2[0-4])$")

#: Named keys that do not fit the one-character form. Windows virtual-key codes.
_NAMED_VK = {
    "SPACE": 0x20,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "TAB": 0x09,
    "BACKSPACE": 0x08,
    "BKSP": 0x08,
    "RETURN": 0x0D,
    "ENTER": 0x0D,
    "INSERT": 0x2D,
    "INS": 0x2D,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PGUP": 0x21,
    "PAGEDOWN": 0x22,
    "PGDOWN": 0x22,
    "PGDN": 0x22,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "PAUSE": 0x13,
    "PRTSC": 0x2C,
    "PRNTSC": 0x2C,
    "SCREENSHOT": 0x2C,
    "MENU": 0x5D,
    "APPS": 0x5D,
}

_USER32 = None


def _user32():
    """The loaded ``user32`` DLL, or ``None`` off Windows / if it will not load."""
    global _USER32
    if _USER32 is None and sys.platform == "win32":
        try:
            _USER32 = ctypes.WinDLL("user32", use_last_error=True)
        except Exception as exc:  # pragma: no cover - only on a broken Windows install
            log.warning("Could not load user32 for hotkey support: %s", exc)
            _USER32 = False
    return _USER32 or None


def _printable_key_to_vk(char: str) -> Optional[tuple[int, bool]]:
    """Map one printable character to ``(vkCode, needsShift)`` via ``VkKeyScanW``.

    ``VkKeyScanW`` is the authority on which virtual key produces a character on the user's own
    keyboard layout, so it is asked rather than assuming US. Its high byte says whether that
    key requires Shift *on this layout*.

    An ASCII letter is looked up in lower case, because ``VkKeyScanW('D')`` reports "needs
    shift" for the character ``D`` — which would silently turn a plain ``Ctrl+Alt+D`` into
    ``Ctrl+Alt+Shift+D``. For any other character the given form is used, because a symbol like
    ``!`` genuinely does need the shift ``VkKeyScanW`` reports for it.
    """
    query = char
    if char.isascii() and char.isalpha():
        query = char.lower()
    user32 = _user32()
    if user32 is not None:
        try:
            fn = user32.VkKeyScanW
            fn.argtypes = [ctypes.c_wchar]
            fn.restype = ctypes.c_short
            packed = int(fn(query))
        except Exception as exc:
            log.debug("VkKeyScanW(%r) failed: %s", query, exc)
            return None
        # -1 means the layout cannot produce this character at all.
        if packed == -1:
            return None
        return packed & 0xFF, bool(packed >> 8)
    # Off Windows (tests, and the Qt-free import path) the ASCII code is the key code for
    # letters and digits, which is exact for everything the table above does not cover.
    return ord(query), False


def _key_token_to_vk(token: str) -> Optional[tuple[int, bool]]:
    """Resolve the non-modifier part of a sequence to ``(vkCode, needsShift)``."""
    if len(token) == 1:
        return _printable_key_to_vk(token)
    upper = token.upper()
    if upper in _NAMED_VK:
        return _NAMED_VK[upper], False
    f_key = _F_KEY_RE.match(upper)
    if f_key:
        return 0x6F + int(f_key.group(1)), False
    return None


def parse_hotkey(sequence: str) -> Optional[tuple[int, int]]:
    """Translate a stored key sequence into Win32 ``(fsModifiers, vkCode)``.

    Returns ``None`` when *sequence* is blank, carries an unknown key or modifier, needs more
    than one chord, or is unsafe to claim globally (see ``_REQUIRED_MODIFIERS``).
    """
    text = (sequence or "").strip()
    if not text:
        return None
    # A multi-step sequence ("Ctrl+K, Ctrl+B") has no single Win32 chord equivalent.
    if "," in text:
        return None

    matched = _SEQUENCE_RE.match(text)
    if matched is None:
        # No modifier prefix at all: the whole string is a bare key, which is never global.
        return None
    fs_modifiers = 0
    for name in matched.group("mods").split("+"):
        if not name:
            continue
        modifier = _MODIFIER_NAMES.get(name.lower())
        if modifier is None:
            return None
        fs_modifiers |= modifier
    if not any(fs_modifiers & required for required in _REQUIRED_MODIFIERS):
        return None

    resolved = _key_token_to_vk(matched.group("key"))
    if resolved is None:
        return None
    vk, needs_shift = resolved
    if needs_shift:
        fs_modifiers |= MOD_SHIFT
    return fs_modifiers, vk


def describe_modifiers(fs_modifiers: int) -> str:
    """Render Win32 ``fsModifiers`` back to a readable prefix, for error messages."""
    parts = []
    if fs_modifiers & MOD_CONTROL:
        parts.append("Ctrl")
    if fs_modifiers & MOD_ALT:
        parts.append("Alt")
    if fs_modifiers & MOD_SHIFT:
        parts.append("Shift")
    if fs_modifiers & MOD_WIN:
        parts.append("Win")
    return "+".join(parts)


class _NativeHotkeyFilter(QAbstractNativeEventFilter):
    """Picks ``WM_HOTKEY`` out of Qt's native event stream and forwards it to *owner*."""

    def __init__(self, owner: HotkeyRegistration):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(ctypes.wintypes.MSG)).contents
        except Exception as exc:
            log.debug("Could not decode native message: %s", exc)
            return False, 0
        if msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
            self._owner._on_hotkey()
            return True, 0
        return False, 0


class HotkeyRegistration(QObject):
    """Owns at most one system-wide hotkey for the lifetime of this object."""

    triggered = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._sequence: str = ""
        self._registered: bool = False
        self._last_error: str = ""
        self._filter = _NativeHotkeyFilter(self)
        self._filter_installed: bool = False

    @property
    def sequence(self) -> str:
        """The chord currently registered, or ``""`` when nothing is."""
        return self._sequence if self._registered else ""

    @property
    def is_registered(self) -> bool:
        return self._registered

    @property
    def last_error(self) -> str:
        """Why the most recent :meth:`register` failed. Empty after a success."""
        return self._last_error

    def register(self, sequence: str) -> tuple[bool, str]:
        """Bind *sequence* system-wide, replacing any existing binding.

        Returns ``(ok, message)``. The message is user-facing: a chord owned by another
        application must say so rather than leaving the user with a hotkey that does nothing.
        """
        text = (sequence or "").strip()
        if not text:
            self.unregister()
            self._last_error = "No hotkey is configured."
            return False, self._last_error
        if sys.platform != "win32":
            self.unregister()
            self._last_error = "Global hotkeys are only available on Windows."
            return False, self._last_error

        if self._registered and text == self._sequence:
            # Already claimed. Dropping and re-claiming would open a window in which the chord
            # belongs to nobody, and if the second claim then failed the hotkey would be gone
            # entirely rather than merely stale - so check this *before* unregistering.
            self._last_error = ""
            return True, f"'{text}' is already a global hotkey."

        self.unregister()
        parsed = parse_hotkey(text)
        if parsed is None:
            self._last_error = (
                f"'{text}' cannot be used as a global hotkey. "
                "Use a single key combination including Ctrl, Alt or Win "
                "(for example Ctrl+Alt+D)."
            )
            return False, self._last_error
        fs_modifiers, vk = parsed

        if not self._ensure_filter():
            self._last_error = "Could not install the native event filter needed for hotkeys."
            return False, self._last_error

        user32 = _user32()
        if user32 is None:
            self._last_error = "Global hotkeys are only available on Windows."
            return False, self._last_error
        try:
            fn = user32.RegisterHotKey
            fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
            fn.restype = ctypes.c_bool
            ok = bool(fn(None, HOTKEY_ID, fs_modifiers, vk))
            error_code = ctypes.get_last_error()
        except Exception as exc:
            log.warning("RegisterHotKey failed: %s", exc)
            self._last_error = f"Could not register '{text}': {exc}"
            return False, self._last_error

        if not ok:
            log.warning(
                "RegisterHotKey(%s, %d) failed with %d",
                describe_modifiers(fs_modifiers), vk, error_code,
            )
            if error_code == ERROR_HOTKEY_ALREADY_REGISTERED:
                self._last_error = (
                    f"'{text}' is already used by another application. "
                    "Choose a different combination."
                )
            else:
                self._last_error = f"'{text}' could not be registered (error {error_code})."
            return False, self._last_error

        self._registered = True
        self._sequence = text
        self._last_error = ""
        log.info("Registered global hotkey %s (mods=0x%X vk=0x%X)", text, fs_modifiers, vk)
        return True, f"'{text}' is now a global hotkey."

    def unregister(self) -> None:
        """Release the binding. Safe to call when nothing is registered."""
        if not self._registered:
            return
        self._registered = False
        self._sequence = ""
        user32 = _user32()
        if user32 is not None:
            try:
                fn = user32.UnregisterHotKey
                fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
                fn.restype = ctypes.c_bool
                if not fn(None, HOTKEY_ID):
                    log.warning("UnregisterHotKey(%d) returned false", HOTKEY_ID)
            except Exception as exc:
                log.warning("UnregisterHotKey failed: %s", exc)

    def _ensure_filter(self) -> bool:
        if self._filter_installed:
            return True
        from PySide6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is None:
            # No event loop can ever deliver WM_HOTKEY, so registering would silently
            # produce a hotkey that never fires.
            return False
        try:
            # PySide6 binds installNativeEventFilter as returning None, so success is "did not
            # raise"; coercing the return to bool would refuse every registration.
            app.installNativeEventFilter(self._filter)
        except Exception as exc:
            log.warning("installNativeEventFilter failed: %s", exc)
            self._filter_installed = False
            return False
        self._filter_installed = True
        return True

    def _on_hotkey(self) -> None:
        log.debug("Global hotkey %s pressed", self._sequence)
        self.triggered.emit()