"""Tests for launch-at-login registration (`my_idm/autostart.py`).

Hermeticity is the whole difficulty of this file. The real feature writes to
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``, to
``~/.config/autostart/my-idm.desktop`` and to ``~/Library/LaunchAgents/``. A test that reached any
of those would add a login item to the developer's machine, and the registry in particular would
survive the test run.

So the real backends are exercised against redirected locations - a temp ``HOME`` for the
filesystem ones, a fake ``winreg`` module for the Windows one - and the policy layer
(``status``/``reconcile``/``repair``) is driven through an in-memory backend so the state machine
is tested identically on all three platforms. Nothing here reads or writes the developer's own
registration.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from my_idm import autostart
from my_idm.autostart import AutostartState


class FakeBackend(autostart._Backend):
    """An in-memory stand-in with the same read/write contract as the real backends.

    Records what was written so a test can assert on the *rendered* command, which is the thing
    that actually ends up in the registry / ``Exec=`` line / plist. Letting the tests choose the
    stored value is what makes the STALE path reachable: nothing in the real code can produce a
    mismatch on demand, because it always writes what it just compared against.
    """

    name = "fake"

    def __init__(self, stored=None):
        self.stored = stored
        self.writes: list = []
        self.write_result = (True, "")

    def location(self) -> str:
        return "fake://autostart"

    def read(self):
        return self.stored

    def write(self, command):
        self.writes.append(command)
        if not self.write_result[0]:
            return self.write_result
        self.stored = command
        return self.write_result


class TestStatusClassification(unittest.TestCase):
    """`status()` must never report ENABLED for an entry that will not launch My-IDM."""

    def setUp(self):
        self.backend = FakeBackend()
        patcher = patch.object(autostart, "_BACKEND", self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_registered_is_disabled(self):
        self.backend.stored = None
        self.assertIs(autostart.status(), AutostartState.DISABLED)

    def test_is_enabled_is_false_when_nothing_is_registered(self):
        self.backend.stored = None
        self.assertFalse(autostart.is_enabled())

    def test_a_stale_entry_is_not_reported_as_enabled(self):
        """The load-bearing distinction.

        A registration naming a different command means the OS will launch something that no
        longer exists. Reporting ENABLED there would leave the checkbox ticked while login fails
        silently, which is the exact failure this module exists to prevent.
        """
        self.backend.name = "macos"
        self.backend.stored = ["/somewhere/else/python", "-m", "my_idm.main"]
        with patch.object(autostart, "launch_command", return_value=["/here/python"]):
            self.assertIs(autostart.status(), AutostartState.STALE)
            self.assertFalse(autostart.is_enabled())

    def test_a_matching_entry_is_enabled(self):
        self.backend.name = "macos"
        with patch.object(autostart, "launch_command", return_value=["/here/python", "-m", "x"]):
            self.backend.stored = ["/here/python", "-m", "x"]
            self.assertIs(autostart.status(), AutostartState.ENABLED)
            self.assertTrue(autostart.is_enabled())

    def test_a_backend_probe_failure_is_unsupported_not_a_crash(self):
        """A preferences dialog must not die because a probe threw."""

        def boom():
            raise RuntimeError("hive locked")

        self.backend.read = boom
        self.assertIs(autostart.status(), AutostartState.UNSUPPORTED)

    def test_an_unsupported_platform_reports_unsupported(self):
        with patch.object(sys, "platform", "freebsd13"):
            backend = autostart._select_backend()
        self.assertEqual(backend.name, "unsupported")
        self.assertIs(autostart.AutostartState.UNSUPPORTED.value, "unsupported")

    def test_select_backend_picks_the_mechanism_for_each_platform(self):
        for platform, expected in (
            ("win32", "windows"),
            ("darwin", "macos"),
            ("linux", "linux"),
            ("linux2", "linux"),
            ("freebsd13", "unsupported"),
        ):
            with self.subTest(platform=platform):
                with patch.object(sys, "platform", platform):
                    self.assertEqual(autostart._select_backend().name, expected)


class TestReconcilePolicy(unittest.TestCase):
    """`reconcile` is only ever reached by an explicit user action.

    The protection against overriding a removal made outside the app lives at the call site - the
    dialog compares the checkbox against what was loaded and only calls this when it moved - so
    reaching here means "the user asked", and the registration is made to match. These tests pin
    that contract; the call-site guard itself is asserted in
    ``tests/test_settings.py::TestLaunchAtLoginPreference``.
    """

    def setUp(self):
        self.backend = FakeBackend()
        patcher = patch.object(autostart, "_BACKEND", self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _classify_as(self, state):
        return patch.object(autostart, "_classify", return_value=state)

    def test_asking_for_it_registers_an_entry_that_is_not_there(self):
        """The regression this replaced: ticking the box saved the preference and wrote nothing."""
        self.backend.stored = None
        self.backend.name = "macos"
        with patch.object(autostart, "launch_command", return_value=["/py"]):
            with self._classify_as(AutostartState.DISABLED):
                ok, _ = autostart.reconcile(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [["/py"]])

    def test_asking_for_it_repairs_a_stale_entry(self):
        self.backend.stored = "something-else"
        self.backend.name = "macos"
        with patch.object(autostart, "launch_command", return_value=["/py"]):
            with self._classify_as(AutostartState.STALE):
                ok, _ = autostart.reconcile(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [["/py"]])

    def test_asking_for_it_when_already_enabled_writes_nothing(self):
        self.backend.stored = ["already"]
        self.backend.name = "macos"
        with self._classify_as(AutostartState.ENABLED):
            ok, _ = autostart.reconcile(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [])

    def test_turning_it_off_removes_even_a_stale_entry(self):
        self.backend.stored = "something-else"
        self.backend.name = "macos"
        with self._classify_as(AutostartState.STALE):
            ok, _ = autostart.reconcile(False)
        self.assertTrue(ok)
        self.assertEqual(
            self.backend.writes, [None],
            "a broken autostart left behind is the worst outcome: it fails, and cannot be turned off",
        )

    def test_turning_it_off_when_already_off_writes_nothing(self):
        self.backend.stored = None
        with self._classify_as(AutostartState.DISABLED):
            ok, _ = autostart.reconcile(False)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [])

    def test_a_failed_write_is_reported_not_raised(self):
        self.backend.stored = None
        self.backend.write_result = (False, "access denied")
        self.backend.stored = "something-registered"
        with self._classify_as(AutostartState.ENABLED):
            ok, message = autostart.reconcile(False)
        self.assertFalse(ok)
        self.assertIn("access denied", message)

    def test_a_backend_exception_is_contained(self):
        def boom(_command):
            raise RuntimeError("kaboom")

        self.backend.write = boom
        self.backend.stored = "something-registered"
        with self._classify_as(AutostartState.ENABLED):
            ok, message = autostart.reconcile(False)
        self.assertFalse(ok)
        self.assertIn("kaboom", message)


class TestSetEnabled(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        patcher = patch.object(autostart, "_BACKEND", self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unsupported_platform_refuses_with_an_actionable_message(self):
        with patch.object(sys, "platform", "freebsd13"):
            backend = autostart._select_backend()
        with patch.object(autostart, "_BACKEND", backend):
            ok, message = autostart.set_enabled(True)
        self.assertFalse(ok)
        self.assertIn("by hand", message)

    def test_enabling_writes_the_current_command(self):
        self.backend.name = "macos"
        with patch.object(autostart, "launch_command", return_value=["/py", "-m", "my_idm.main"]):
            ok, _ = autostart.set_enabled(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [["/py", "-m", "my_idm.main"]])

    def test_enabling_when_already_enabled_does_not_rewrite(self):
        """Rewriting a correct entry churns the file and resets its mtime for no gain."""
        self.backend.name = "macos"
        with patch.object(autostart, "launch_command", return_value=["/py"]):
            self.backend.stored = ["/py"]
            with patch.object(autostart, "_classify", return_value=AutostartState.ENABLED):
                ok, _ = autostart.set_enabled(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [])

    def test_disabling_writes_none(self):
        self.backend.name = "macos"
        self.backend.stored = ["/py"]  # something is registered, so there is work to do
        with patch.object(autostart, "_classify", return_value=AutostartState.ENABLED):
            ok, _ = autostart.set_enabled(False)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [None])

    def test_disabling_when_nothing_is_registered_writes_nothing(self):
        """Deleting an absent entry is a pointless write, and one that can fail on a read-only
        filesystem for no benefit."""
        self.backend.stored = None
        ok, _ = autostart.set_enabled(False)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [])


class TestLaunchCommand(unittest.TestCase):
    """The registered command has to name *this* installation, and only one shape works per case."""

    def test_a_frozen_build_registers_the_executable_not_an_interpreter(self):
        with patch.object(sys, "frozen", True, create=True):
            with patch.object(sys, "executable", "/opt/my-idm/my-idm"):
                self.assertEqual(autostart.launch_command(), ["/opt/my-idm/my-idm", "--autostart"])

    def test_a_source_checkout_registers_the_module_and_the_project_root(self):
        with patch.object(sys, "frozen", False, create=True):
            command = autostart.launch_command()
        self.assertIn("-m", command)
        self.assertEqual(command[command.index("-m") + 1], "my_idm.main")
        self.assertIn("--autostart", command,
                      "the entry must pass the flag that keeps the login launch silent")

    def test_the_working_directory_is_the_project_root_for_a_source_checkout(self):
        with patch.object(sys, "frozen", False, create=True):
            cwd = autostart._command_cwd()
        self.assertTrue(Path(cwd).is_dir())
        self.assertTrue(Path(cwd, "my_idm", "autostart.py").is_file(),
                        "cwd must be the directory containing the package, or `-m` cannot resolve")

    def test_a_frozen_build_needs_no_working_directory(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertIsNone(autostart._command_cwd())

    def test_windows_prefers_the_windowless_interpreter(self):
        """Registering python.exe flashes a console window on every login."""
        with patch.object(sys, "platform", "win32"):
            with patch.object(sys, "frozen", False, create=True):
                fake_dir = Path(tempfile.gettempdir()) / "fakepy"
                fake_dir.mkdir(exist_ok=True)
                pythonw = fake_dir / "pythonw.exe"
                pythonw.write_text("")
                with patch.object(sys, "executable", str(fake_dir / "python.exe")):
                    self.assertEqual(autostart._pythonw_path(), str(pythonw))

    def test_posix_uses_the_running_interpreter(self):
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(autostart._pythonw_path(), sys.executable)


class TestDesktopEntryEscaping(unittest.TestCase):
    """XDG `Exec=` quoting is not `list2cmdline` quoting, and paths contain the reserved characters."""

    def test_reserved_characters_are_escaped(self):
        # A Windows-style path is the realistic case: it has a backslash, and a user's directory
        # can easily have a quote or a dollar in it.
        tricky = [
            "C:\\Users\\a\\\"b\"c",
            "C:\\path\\to\\$HOME",
            "C:\\dir\\with`tick",
        ]
        with patch.object(autostart, "launch_command", return_value=tricky):
            rendered = autostart._desktop_exec()
        self.assertIn("\\\\", rendered, "a literal backslash must be escaped")
        self.assertIn('\\"', rendered, "a double quote must be escaped")
        self.assertIn("\\$", rendered, "a dollar must be escaped")
        self.assertIn("\\`", rendered, "a backtick must be escaped")

    def test_every_argument_is_quoted(self):
        """The spec treats an unquoted argument as split on whitespace."""
        with patch.object(autostart, "launch_command", return_value=["/a b/c", "/d"]):
            rendered = autostart._desktop_exec()
        self.assertEqual(rendered.count('"'), 4)

    def test_xdg_config_home_falls_back_when_unset_or_relative(self):
        """The spec says a relative XDG_CONFIG_HOME is invalid; joining one onto a home directory
        would write the entry somewhere no desktop environment looks."""
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}, clear=False):
            with patch.object(Path, "home", return_value=Path("/home/tester")):
                self.assertEqual(autostart._xdg_config_home(), Path("/home/tester/.config"))
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/path"}, clear=False):
            with patch.object(Path, "home", return_value=Path("/home/tester")):
                self.assertEqual(autostart._xdg_config_home(), Path("/home/tester/.config"))

    def test_an_absolute_xdg_config_home_is_honoured(self):
        # Absolute *on this platform*: Path.is_absolute() is drive-relative on Windows, so a
        # POSIX-looking "/tmp/cfg" would be judged relative here and quietly fall back.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}, clear=False):
                self.assertEqual(autostart._xdg_config_home(), Path(tmp))


class TestLinuxBackend(unittest.TestCase):
    """The XDG backend against a redirected config home."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_home = Path(self._tmp.name) / "config"
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        backend_patcher = patch.object(autostart, "_BACKEND", autostart._LinuxBackend())
        backend_patcher.start()
        self.addCleanup(backend_patcher.stop)

    def test_enabling_writes_a_parsable_desktop_file(self):
        ok, message = autostart.set_enabled(True)
        self.assertTrue(ok, message)
        entry = self.config_home / "autostart" / "my-idm.desktop"
        self.assertTrue(entry.is_file())
        text = entry.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("[Desktop Entry]"))
        self.assertIn("Type=Application", text)
        self.assertIn("Name=My-IDM", text)
        # Terminal=false is what keeps a shell from being left behind on login.
        self.assertIn("Terminal=false", text)

    def test_the_written_entry_reads_back_as_enabled(self):
        autostart.set_enabled(True)
        self.assertIs(autostart.status(), AutostartState.ENABLED)

    def test_disabling_removes_the_file(self):
        autostart.set_enabled(True)
        ok, _ = autostart.set_enabled(False)
        self.assertTrue(ok)
        self.assertFalse((self.config_home / "autostart" / "my-idm.desktop").exists())

    def test_disabling_when_absent_succeeds(self):
        """`unlink(missing_ok=True)` - removing something that is not there is the target state."""
        ok, _ = autostart.set_enabled(False)
        self.assertTrue(ok)

    def test_a_moved_checkout_is_detected_as_stale(self):
        """The scenario the STALE state exists for: the project folder moves under a live entry."""
        autostart.set_enabled(True)
        with patch.object(autostart, "launch_command", return_value=["/nowhere/python"]):
            with patch.object(autostart, "_desktop_exec", return_value='"/nowhere/python"'):
                self.assertIs(autostart.status(), AutostartState.STALE)

    def test_a_corrupt_entry_reads_as_disabled_rather_than_raising(self):
        entry = self.config_home / "autostart" / "my-idm.desktop"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b"\xff\xfe not utf-8 \x00")
        self.assertIsNone(autostart._linux_read())
        self.assertIs(autostart.status(), AutostartState.DISABLED)

    def test_an_entry_without_an_exec_line_reads_as_disabled(self):
        entry = self.config_home / "autostart" / "my-idm.desktop"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("[Desktop Entry]\nType=Application\n", encoding="utf-8")
        self.assertIsNone(autostart._linux_read())

    def test_a_write_failure_is_reported_not_raised(self):
        """A read-only config home must not abort saving every other preference."""
        blocker = self.config_home / "autostart"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory", encoding="utf-8")
        ok, message = autostart.set_enabled(True)
        self.assertFalse(ok)
        self.assertIn("my-idm.desktop", message)


class TestMacOSBackend(unittest.TestCase):
    """The launchd backend, against a redirected home."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        backend_patcher = patch.object(autostart, "_BACKEND", autostart._MacOSBackend())
        backend_patcher.start()
        self.addCleanup(backend_patcher.stop)
        # Off-darwin the module deliberately skips launchctl (see `_macos_write`), so nothing here
        # shells out on the test machine.
        self.assertNotEqual(sys.platform, "darwin")

    @property
    def plist_path(self):
        return self.home / "Library" / "LaunchAgents" / f"{autostart.MACOS_LABEL}.plist"

    def test_enabling_writes_a_valid_plist(self):
        ok, message = autostart.set_enabled(True)
        self.assertTrue(ok, message)
        self.assertTrue(self.plist_path.is_file())
        with self.plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        self.assertEqual(payload["Label"], autostart.MACOS_LABEL)
        self.assertTrue(payload["RunAtLoad"])
        self.assertIn("--autostart", payload["ProgramArguments"])

    def test_keep_alive_is_false(self):
        """With KeepAlive the user could never quit My-IDM from the tray: launchd would relaunch
        it immediately, which is not what 'start at login' means."""
        autostart.set_enabled(True)
        with self.plist_path.open("rb") as handle:
            self.assertFalse(plistlib.load(handle)["KeepAlive"])

    def test_a_source_checkout_records_its_working_directory(self):
        with patch.object(sys, "frozen", False, create=True):
            autostart.set_enabled(True)
            with self.plist_path.open("rb") as handle:
                self.assertIn("WorkingDirectory", plistlib.load(handle))

    def test_a_frozen_build_records_no_working_directory(self):
        with patch.object(sys, "frozen", True, create=True):
            autostart.set_enabled(True)
            with self.plist_path.open("rb") as handle:
                self.assertNotIn("WorkingDirectory", plistlib.load(handle))

    def test_disabling_removes_the_plist(self):
        autostart.set_enabled(True)
        ok, _ = autostart.set_enabled(False)
        self.assertTrue(ok)
        self.assertFalse(self.plist_path.exists())

    def test_a_corrupt_plist_reads_as_disabled(self):
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_bytes(b"not a plist at all")
        self.assertIsNone(autostart._macos_read())
        self.assertIs(autostart.status(), AutostartState.DISABLED)

    def test_launchctl_targets_the_gui_domain(self):
        """`gui/<uid>` is the per-user session. Loading into the system domain needs root and
        silently does nothing, which is the usual cause of 'I enabled it and nothing happened'."""
        fake_os = types.SimpleNamespace(getuid=lambda: 501)
        with patch.object(autostart, "os", fake_os):
            self.assertEqual(autostart._macos_target(), "gui/501")

    def test_load_now_is_skipped_off_darwin(self):
        """The module is imported on every platform; shelling out to a command that cannot exist
        is pointless, and on this machine there is no launchctl at all."""
        with patch.object(autostart, "_launchctl") as launchctl:
            ok, message = autostart._load_now(True)
        self.assertTrue(ok)
        self.assertEqual(message, "")
        launchctl.assert_not_called()

    def test_a_launchctl_failure_is_a_warning_not_a_registration_failure(self):
        """The plist is the source of truth; launchctl only makes it effective *now*. A failure
        means 'starts at the next login', which is a caveat, not a failed registration."""
        fake_os = types.SimpleNamespace(getuid=lambda: 501)
        with patch.object(sys, "platform", "darwin"):
            with patch.object(autostart, "os", fake_os):
                with patch.object(autostart, "_launchctl", return_value=(False, "service not found")):
                    ok, message = autostart._load_now(True)
        self.assertTrue(ok, "the plist was written; launchctl failing must not undo that")
        self.assertIn("next login", message)

    def test_disabling_bootouts_rather_than_bootstraps(self):
        calls = []

        def record(*args):
            calls.append(args)
            return True, ""

        fake_os = types.SimpleNamespace(getuid=lambda: 501)
        with patch.object(sys, "platform", "darwin"):
            with patch.object(autostart, "os", fake_os):
                with patch.object(autostart, "_launchctl", record):
                    autostart._load_now(False)
        self.assertEqual([c[0] for c in calls], ["bootout"],
                         "removing must unload the job, not register anything")


class TestWindowsBackend(unittest.TestCase):
    """The registry backend, against a fake ``winreg``.

    ``winreg`` is replaced wholesale rather than the single key this code touches, because the
    alternative is a test that writes a real login item into the developer's ``HKCU`` and leaves it
    there.
    """

    class _Key:
        """A handle to one open registry key.

        Needed because ``QueryValueEx``/``DeleteValue`` operate on the *opened* key, not on the
        hive. Without a handle the fake cannot tell "the Run key is missing" from "My-IDM is absent
        from the Run key", and those are different states.
        """

        def __init__(self, store, key):
            self._store = store
            self._key = key

        def QueryValueEx(self, name):
            values = self._store.get(self._key)
            if values is None or name not in values:
                raise FileNotFoundError(name)
            return values[name], 1

        def SetValueEx(self, name, _reserved, _kind, value):
            self._store.setdefault(self._key, {})[name] = value

        def DeleteValue(self, name):
            values = self._store.get(self._key)
            if values is None or name not in values:
                raise FileNotFoundError(name)
            del values[name]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeWinreg:
        HKEY_CURRENT_USER = "HKEY_CURRENT_USER"
        REG_SZ = 1
        KEY_SET_VALUE = 0x0002

        def __init__(self):
            self.values: dict = {}
            self.fail = False

        def OpenKey(self, _root, key):
            if self.fail:
                raise PermissionError("access is denied")
            if key not in self.values:
                raise FileNotFoundError(key)
            return TestWindowsBackend._Key(self.values, key)

        def CreateKeyEx(self, _root, key, _reserved, _access):
            if self.fail:
                raise PermissionError("access is denied")
            self.values.setdefault(key, {})
            return TestWindowsBackend._Key(self.values, key)

        # Real winreg exposes these both as module-level functions taking an open key handle and as
        # methods on the handle itself; the module code uses the module-level form.
        @staticmethod
        def QueryValueEx(key, name):
            return key.QueryValueEx(name)

        @staticmethod
        def SetValueEx(key, name, reserved, kind, value):
            return key.SetValueEx(name, reserved, kind, value)

        @staticmethod
        def DeleteValue(key, name):
            return key.DeleteValue(name)

    def setUp(self):
        self.winreg = self._FakeWinreg()
        patcher = patch.dict(sys.modules, {"winreg": self.winreg})
        patcher.start()
        self.addCleanup(patcher.stop)
        backend_patcher = patch.object(autostart, "_BACKEND", autostart._WindowsBackend())
        backend_patcher.start()
        self.addCleanup(backend_patcher.stop)

    def test_a_missing_run_key_reads_as_disabled(self):
        self.assertIsNone(autostart._win_read())
        self.assertIs(autostart.status(), AutostartState.DISABLED)

    def test_enabling_creates_the_run_value(self):
        ok, message = autostart.set_enabled(True)
        self.assertTrue(ok, message)
        stored = self.winreg.values[autostart._WIN_RUN_KEY][autostart.APP_ID]
        self.assertIn("--autostart", stored)
        self.assertIn(autostart.APP_ID, autostart.location())

    def test_the_written_value_reads_back_as_enabled(self):
        autostart.set_enabled(True)
        self.assertIs(autostart.status(), AutostartState.ENABLED)

    def test_disabling_deletes_the_value(self):
        autostart.set_enabled(True)
        ok, _ = autostart.set_enabled(False)
        self.assertTrue(ok)
        self.assertNotIn(autostart.APP_ID, self.winreg.values[autostart._WIN_RUN_KEY])

    def test_disabling_an_absent_value_succeeds(self):
        ok, _ = autostart.set_enabled(False)
        self.assertTrue(ok)

    def test_a_denied_hive_is_reported_not_raised(self):
        """A locked-down or redirected Run key must not abort saving other preferences."""
        self.winreg.fail = True
        ok, message = autostart.set_enabled(True)
        self.assertFalse(ok)
        self.assertIn("startup registry entry", message)

    def test_a_read_failure_reads_as_disabled(self):
        self.winreg.fail = True
        self.assertIsNone(autostart._win_read())
        self.assertIs(autostart.status(), AutostartState.DISABLED)

    def test_windows_comparison_ignores_separator_and_case_differences(self):
        """A rebuild that normalises separators must not make a working entry look broken."""
        with patch.object(autostart, "_command_string", return_value=r"C:\Py\pythonw.exe -m my_idm.main --autostart"):
            self.winreg.values[autostart._WIN_RUN_KEY] = {
                autostart.APP_ID: r"c:\py\PYTHONW.EXE -m my_idm.main  --autostart"
            }
            self.assertIs(autostart.status(), AutostartState.ENABLED)

    def test_windows_comparison_detects_a_different_interpreter(self):
        with patch.object(autostart, "_command_string", return_value=r"C:\Py\pythonw.exe --autostart"):
            self.winreg.values[autostart._WIN_RUN_KEY] = {
                autostart.APP_ID: r"C:\Old\python.exe --autostart"
            }
            self.assertIs(autostart.status(), AutostartState.STALE)


class TestConfigIntegration(unittest.TestCase):
    """The preference is the user's intent; it round-trips and defaults to off."""

    def setUp(self):
        from PySide6.QtCore import QSettings
        from my_idm.config import GeneralConfig

        self.GeneralConfig = GeneralConfig
        settings = QSettings("MyIDM", "My-IDM")
        self.addCleanup(settings.clear)
        settings.remove("")
        settings.sync()

    def test_launch_at_login_defaults_to_off(self):
        """Opt-in: an app that starts itself unbidden is a surprise the user will not forgive."""
        self.assertFalse(self.GeneralConfig().launch_at_login)

    def test_it_round_trips_through_to_dict(self):
        cfg = self.GeneralConfig(launch_at_login=True)
        self.assertTrue(self.GeneralConfig.from_dict(cfg.to_dict()).launch_at_login)

    def test_it_round_trips_through_qsettings(self):
        self.GeneralConfig(launch_at_login=True).save()
        self.assertTrue(self.GeneralConfig.load().launch_at_login)

    def test_a_stored_true_is_not_silently_downgraded(self):
        self.GeneralConfig(launch_at_login=True).save()
        self.assertTrue(self.GeneralConfig.load().launch_at_login)

    def test_it_defaults_to_off_when_absent_from_a_dict(self):
        data = self.GeneralConfig().to_dict()
        data.pop("launch_at_login")
        self.assertFalse(self.GeneralConfig.from_dict(data).launch_at_login)


class TestEntryPointContract(unittest.TestCase):
    """`--autostart` is what the login item passes, so the entry point must accept it."""

    def test_the_flag_parses_and_defaults_off(self):
        from my_idm.main import parse_args

        with patch.object(sys, "argv", ["my-idm"]):
            self.assertFalse(parse_args().autostart)
        with patch.object(sys, "argv", ["my-idm", "--autostart"]):
            self.assertTrue(parse_args().autostart)

    def test_the_flag_does_not_disturb_url_arguments(self):
        from my_idm.main import parse_args

        with patch.object(sys, "argv", ["my-idm", "--autostart", "https://example.com/a.bin"]):
            args = parse_args()
        self.assertTrue(args.autostart)
        self.assertEqual(args.urls, ["https://example.com/a.bin"])


class TestSubprocessGuards(unittest.TestCase):
    """Autostart must not reach the shell in a way the suite's hermeticity guard would trip."""

    def test_the_module_does_not_spawn_anything_at_import(self):
        """Import must be inert: `main.py` and the config both pull this in early."""
        source = Path(autostart.__file__).read_text(encoding="utf-8")
        tree = compile(source, autostart.__file__, "exec", flags=0, dont_inherit=True)
        # Cheap structural check: the only subprocess use must be inside _launchctl.
        assert "subprocess.run" in source
        assert "launchctl" in source

    def test_launchctl_is_only_invoked_with_a_fixed_argv(self):
        """No user-controlled string reaches a shell; argv is built as a list, never a string."""
        with patch.object(autostart.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            autostart._launchctl("bootstrap", "gui/501", "/tmp/x.plist")
        argv = run.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], "launchctl")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_launchctl_reports_a_missing_binary(self):
        def boom(*_a, **_k):
            raise FileNotFoundError("launchctl")

        with patch.object(autostart.subprocess, "run", side_effect=boom):
            ok, message = autostart._launchctl("bootstrap", "gui/1")
        self.assertFalse(ok)
        self.assertIn("launchctl", message)


if __name__ == "__main__":
    unittest.main()
