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
import ctypes.wintypes
import logging
import re
import sys
from typing import Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

log = logging.getLogger("my_idm")


class _MSG(ctypes.Structure):
    """Win32 ``MSG``, spelled out rather than taken from ``ctypes.wintypes``.

    ``ctypes.wintypes`` is a submodule: ``import ctypes`` alone does **not** make
    ``ctypes.wintypes`` exist, it only appears once some module has imported it. This one is
    used at *message decode* time inside a native event filter, where the difference is
    between a hotkey that works and one that silently doesn't — the failure shows up as
    "module 'ctypes' has no attribute 'wintypes'" once per native message, which reads like a
    crash in Qt rather than a missing import, and is invisible in the UI.

    Only the fields the filter needs are declared; the rest of ``MSG`` is padding the decoder
    skips.
    """

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.wintypes.WPARAM),
        ("lParam", ctypes.wintypes.LPARAM),
        ("time", ctypes.c_uint),
    ]

#: Process-private hotkey ids, one per binding. Arbitrary, but they must not collide with an
#: id anything else in this process might register — nothing else here does.
HOTKEY_ID = 0xB101
CLIPBOARD_HOTKEY_ID = 0xB102

WM_HOTKEY = 0x0312

#: ``RegisterHotKey`` failure meaning another application already owns this chord.
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

#: The two binding names :class:`HotkeyManager` dispatches on.
BINDING_CAPTURE = "capture"
BINDING_CLIPBOARD = "clipboard"

#: Binding name -> the id registered for it. An id only has to be unique inside this
#: process, so these are arbitrary; they are named so nothing else has to know the numbers.
_BINDING_IDS = {BINDING_CAPTURE: HOTKEY_ID, BINDING_CLIPBOARD: CLIPBOARD_HOTKEY_ID}

#: Win32 lock keys. Num Lock is 0x90, Scroll Lock is 0x91.
VK_NUMLOCK = 0x90
VK_SCROLLLOCK = 0x91

#: The only keys a capture hotkey may be bound to, in the order the Preferences drop-down
#: lists them, with the spelling stored in the config. Anything else — chords included — is
#: refused, because the settings these keys drive are not toggles: a lock key carries state
#: the app reads, and *that* is the switch.
LOCK_KEYS = ("ScrollLock", "NumLock")

#: How those keys are shown to a user. The stored spelling is the canonical one, so the
#: drop-down's text and the stored value need one map between them rather than a second
#: opinion about spelling.
LOCK_KEY_LABELS = {"ScrollLock": "Scroll Lock", "NumLock": "Num Lock"}

#: The default for both capture hotkeys. Scroll Lock because it is the lock key nothing else
#: on a desktop is likely to be watching, so it is the one whose LED the user can safely
#: repurpose.
DEFAULT_LOCK_KEY = "ScrollLock"

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


def lock_key_vk(sequence) -> Optional[int]:
    """The virtual key for a lock-key *sequence*, or ``None`` if it is not one.

    Both spellings are accepted — Qt writes them without a space, and so do I in the stored
    config, while people type them with one. Anything that is not even a string is not a key,
    which is what makes this safe to call on values read back from a settings file.
    """
    if not isinstance(sequence, str):
        return None
    token = sequence.strip().upper().replace(" ", "")
    return {
        "SCROLLLOCK": VK_SCROLLLOCK,
        "NUMLOCK": VK_NUMLOCK,
    }.get(token)


def lock_key_is_on(vk: int) -> bool:
    """Is the lock key with virtual key *vk* currently on?

    ``GetKeyState``'s low bit *is* the toggle state for the lock keys, which is what makes
    them usable as a hardware switch for a setting: the LED and the state are the same thing,
    not two things to keep in step.
    """
    if not vk or sys.platform != "win32":
        return False
    user32 = _user32()
    if user32 is None:
        return False
    try:
        fn = user32.GetKeyState
        fn.argtypes = [ctypes.c_int]
        fn.restype = ctypes.c_short
        return bool(fn(vk) & 0x0001)
    except Exception as exc:
        log.warning("GetKeyState(%s) failed: %s", vk, exc)
        return False


def press_lock_key(vk: int) -> bool:
    """Synthesise one press of the lock key with virtual key *vk*.

    A press is a key-down plus a key-up. Callers that only want a *change* should read first
    and call this only when the state is wrong — see :func:`set_lock_key`.

    Synthetic input triggers registered hotkeys like any other, so the caller must expect the
    ``WM_HOTKEY`` for its own chord to come back. That is the caller's echo to ignore.

    Returns whether the press was synthesised — not the resulting state, which for a pressed
    key is the *opposite* of what was wanted and would read as failure to a caller holding
    "did that work?" as a boolean.
    """
    if not vk or sys.platform != "win32":
        return False
    user32 = _user32()
    if user32 is None:
        return False
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    try:
        press = user32.keybd_event
        press.argtypes = [ctypes.c_byte, ctypes.c_byte, ctypes.c_uint, ctypes.c_void_p]
        press.restype = None
        press(vk, 0, KEYEVENTF_EXTENDEDKEY, None)
        press(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, None)
    except Exception as exc:
        log.warning("keybd_event(%s) failed: %s", vk, exc)
        return False
    return True


def set_lock_key(vk: int, on: bool) -> bool:
    """Drive the lock key with virtual key *vk* to *on*, synthesising a press only if needed.

    The read-first is what keeps this from being a *toggle*: synthesising a press on a key that
    is already in the wanted state would flip it, which for Scroll Lock means the LED and the
    feature end up in opposite states — and the resulting ``WM_HOTKEY`` would then flip the
    feature straight back, so the two would ping-pong forever.
    """
    if not vk or sys.platform != "win32":
        return False
    if lock_key_is_on(vk) == bool(on):
        return True
    return press_lock_key(vk)


def lock_key_state(sequence: str) -> Optional[bool]:
    """The current on/off state of the lock key *sequence*, or ``None`` if it is not one."""
    vk = lock_key_vk(sequence)
    return None if vk is None else lock_key_is_on(vk)


def canonical_lock_key(sequence) -> Optional[str]:
    """The canonical spelling of a lock-key *sequence*, however it was written.

    One answer to "which key is this", shared by the config, the Preferences drop-down and
    the tray labels so the same key is not stored one way and shown another.
    """
    vk = lock_key_vk(sequence)
    return {VK_SCROLLLOCK: "ScrollLock", VK_NUMLOCK: "NumLock"}.get(vk) if vk else None


def parse_hotkey(sequence: str) -> Optional[tuple[int, int]]:
    """Translate a stored key sequence into Win32 ``(fsModifiers, vkCode)``.

    *Only* a lock key resolves. The capture hotkeys deliberately stopped being ordinary
    chords: a chord has one meaning regardless of anything else, while a lock key carries
    state the app can read, and it is that state the capture settings follow. A chord would
    imply a toggle, which is what these are not.

    Returns ``None`` for anything else, including every ordinary chord.
    """
    vk = lock_key_vk(sequence)
    if vk is None:
        return None
    return 0, vk


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
            # `_MSG` rather than `ctypes.wintypes.MSG`: see the struct's docstring. The
            # module-level type keeps this working in the app, where `ctypes.wintypes` may
            # never have been imported.
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
        except Exception as exc:
            log.debug("Could not decode native message: %s", exc)
            return False, 0
        if msg.message == WM_HOTKEY and int(msg.wParam) == self._owner._hotkey_id:
            self._owner._on_hotkey()
            return True, 0
        return False, 0


class HotkeyRegistration(QObject):
    """Owns at most one system-wide hotkey for the lifetime of this object.

    *hotkey_id* is the process-private number registered alongside the chord; ``RegisterHotKey``
    echoes it back in ``WM_HOTKEY``, so two bindings need two ids. It defaults to
    :data:`HOTKEY_ID` because the first binding in the app predates the second.
    """

    triggered = Signal()

    def __init__(self, hotkey_id: int = HOTKEY_ID, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._hotkey_id = int(hotkey_id)
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
                f"'{text}' cannot be used as a capture hotkey. "
                "Use Scroll Lock or Num Lock: the capture follows the key's own "
                "on / off state, so a chord cannot do it."
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
            ok = bool(fn(None, self._hotkey_id, fs_modifiers, vk))
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
                if not fn(None, self._hotkey_id):
                    log.warning("UnregisterHotKey(%d) returned false", self._hotkey_id)
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


class HotkeyManager(QObject):
    """The app's system-wide hotkeys: one binding per purpose, told apart by id.

    Each binding is its own :class:`HotkeyRegistration` — and therefore its own native event
    filter and its own process-private id — because ``RegisterHotKey`` takes an id per
    registration and hands that same id back in the ``WM_HOTKEY`` message. Two bindings is
    two ids, not one id shared two ways.

    ``triggered`` carries the binding's *name* rather than being a bare signal, so a handler
    can tell "the browser-capture key was pressed" from "the clipboard key was pressed"
    without asking the config which sequence belongs to which toggle. `register` is the only
    way in: a binding is registered with the sequence the user stored, and disabled by
    unregistering, which is the same policy the single-binding path always had.

    **The same chord may be bound to more than one purpose.** Win32 refuses a second
    ``RegisterHotKey`` of an identical ``fsModifiers``/``vk`` pair for the calling thread with
    ``ERROR_HOTKEY_ALREADY_REGISTERED`` — so a user who assigns Scroll Lock to both captures
    used to get one working binding and one silent failure that blamed another application
    for owning the key. A shared chord is now registered once and dispatched to *every*
    purpose bound to it, which is the only reading of that setting that is not a lie.
    """

    triggered = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._registrations: dict[str, HotkeyRegistration] = {
            key: HotkeyRegistration(hotkey_id, self) for key, hotkey_id in _BINDING_IDS.items()
        }
        # (fsModifiers, vk) -> the binding names that asked for it. The first one owns the
        # Win32 registration; the rest are routed through it on each press.
        self._chords: dict[tuple[int, int], set[str]] = {}
        for key, registration in self._registrations.items():
            # `key` is captured by name, not by the loop variable: the late-binding closure
            # would otherwise make every binding report the last one.
            registration.triggered.connect(
                lambda key=key: self._on_binding_triggered(key)
            )

    def _on_binding_triggered(self, owner: str) -> None:
        """Re-emit the press once per purpose bound to *owner*'s chord."""
        chord = self._chord_of(owner)
        if chord is None:
            self.triggered.emit(owner)
            return
        for key in sorted(self._chords[chord]):
            self.triggered.emit(key)

    def _chord_of(self, key: str) -> Optional[tuple[int, int]]:
        for chord, keys in self._chords.items():
            if key in keys:
                return chord
        return None

    def registration(self, key: str) -> HotkeyRegistration:
        """The :class:`HotkeyRegistration` owning binding *key*.

        Raises ``KeyError`` for an unknown binding: silently returning the wrong one would
        let a caller unregister a hotkey the user never touched.
        """
        if key not in self._registrations:
            raise KeyError(f"unknown hotkey binding: {key!r}")
        return self._registrations[key]

    def register(self, key: str, sequence: str) -> tuple[bool, str]:
        """Bind *key* to *sequence* system-wide. See :meth:`HotkeyRegistration.register`.

        An identical chord already held by another binding is **shared**, not a conflict: only
        one Win32 registration exists for it and a press is emitted for every purpose bound
        to it. The returned message still says so, because a silently shared key is how the
        user ends up believing two keys are in play when there is one.
        """
        parsed = parse_hotkey(sequence)
        if parsed is None:
            return self.registration(key).register(sequence)
        owner = self._owner_of(parsed)
        if owner is not None and owner != key:
            self._chords[parsed].add(key)
            return True, (
                f"'{sequence}' is also the capture key for another capture, "
                "so it drives both."
            )
        if owner == key:
            # Already holding this very chord, so registering again is a no-op Win32.
            return True, ""
        ok, message = self.registration(key).register(sequence)
        if ok:
            self._chords.setdefault(parsed, set()).add(key)
        return ok, message

    def _owner_of(self, chord: tuple[int, int]) -> Optional[str]:
        """Which binding holds the live Win32 registration for *chord*, if any.

        Only a registration that is actually registered counts. A sharer is in ``_chords``
        while owning nothing, so treating it as the owner made the hand-off path believe the
        chord was already claimed and skip the registration it was supposed to make.
        """
        for key in sorted(self._chords.get(chord, ())):
            if self.registration(key).is_registered:
                return key
        return None

    def unregister(self, key: str) -> None:
        registration = self.registration(key)
        chord = self._chord_of(key)
        if chord is None:
            registration.unregister()
            return
        self._chords[chord].discard(key)
        remaining = self._chords[chord]
        if not remaining:
            # This key was the last one asking for the chord.
            del self._chords[chord]
            registration.unregister()
            return
        if registration.is_registered:
            # Others still want this chord, so hand the registration to one of them rather
            # than taking the key away entirely: the sharer keeps working. Read the chord
            # back *before* unregistering, which clears the registration's copy of it.
            sequence = registration.sequence
            registration.unregister()
            heir = sorted(remaining)[0]
            _ok, _message = self.registration(heir).register(sequence)

    def unregister_all(self) -> None:
        """Release every binding. Safe to call when nothing is registered."""
        for registration in self._registrations.values():
            registration.unregister()
        self._chords.clear()

    @property
    def is_any_registered(self) -> bool:
        return any(item.is_registered for item in self._registrations.values())

    def is_binding_registered(self, key: str) -> bool:
        """Is *key* live system-wide? True for a shared chord's owner **and** its sharers.

        ``HotkeyRegistration.is_registered`` answers a narrower question — "does this object
        own the Win32 registration?" — which is False for a sharer by design. Reading that
        here would report a configured, working hotkey as unregistered.
        """
        registration = self.registration(key)
        chord = self._chord_of(key)
        if chord is None:
            return registration.is_registered
        owner = self._owner_of(chord)
        return owner is not None and self.registration(owner).is_registered

    def bindings_for(self, sequence: str) -> set[str]:
        """The binding names that resolve *sequence* to, shared chord included."""
        parsed = parse_hotkey(sequence)
        if parsed is None:
            return set()
        return set(self._chords.get(parsed, ()))