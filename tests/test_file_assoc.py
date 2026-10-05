"""Tests for the .torrent file-association registration.

The interesting property of this feature is what it *refuses* to claim. An application cannot make
itself the default handler for a file type on Windows, and a program that reports "on" anyway is
worse than one that offers nothing — so most of these tests are about the state machine
distinguishing registered-but-not-default from genuinely default, and about `reconcile` not
resurrecting a registration the user removed.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from my_idm import file_assoc
from my_idm.autostart import app_command, launch_command
from my_idm.file_assoc import (
    FileAssocState,
    LINUX_DESKTOP_NAME,
    TORRENT_MIME_TYPE,
    WINDOWS_PROG_ID,
)


class _RecordingBackend(file_assoc._Backend):
    """A stand-in platform backend that stores whatever it is handed."""

    name = "fake"
    supported = True

    def __init__(self):
        self.stored = None
        self.writes = []
        self.default = False
        self.raise_on_read = False

    def location(self):
        return "/somewhere/fake"

    def read(self):
        if self.raise_on_read:
            raise RuntimeError("probe exploded")
        return self.stored

    def is_default(self):
        return self.default

    def write(self, command):
        self.writes.append(command)
        self.stored = command
        return True, ""


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.backend = _RecordingBackend()
        patcher = patch.object(file_assoc, "_BACKEND", self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(file_assoc, "_expected_command", return_value="CMD")
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def test_nothing_registered_is_disabled(self):
        self.assertIs(file_assoc.status(), FileAssocState.DISABLED)
        self.assertFalse(file_assoc.is_enabled())

    def test_a_matching_entry_is_registered(self):
        self.backend.stored = "CMD"
        self.assertIs(file_assoc.status(), FileAssocState.REGISTERED)
        self.assertTrue(file_assoc.is_enabled())
        self.assertFalse(file_assoc.is_default())

    def test_registered_and_the_os_default_is_the_only_default(self):
        # The distinction the whole module exists for. Collapsing these two is how a settings
        # dialog ends up claiming double-clicking a .torrent works when it does not.
        self.backend.stored = "CMD"
        self.backend.default = True
        self.assertIs(file_assoc.status(), FileAssocState.DEFAULT)
        self.assertTrue(file_assoc.is_default())

    def test_an_entry_naming_something_else_is_stale(self):
        self.backend.stored = "SOME_OTHER_PATH --autostart"
        self.assertIs(file_assoc.status(), FileAssocState.STALE)
        # STALE is False here on purpose: reconcile() short-circuits on is_enabled(), so a broken
        # entry answering True would make "repair" a no-op and the stale command would survive.
        self.assertFalse(file_assoc.is_enabled())

    def test_an_unsupported_platform_says_so(self):
        # `supported`, not the name: macOS is a real platform this module recognises and has no
        # mechanism for, so a name-based check would report it as merely "not registered".
        self.backend.supported = False
        self.assertIs(file_assoc.status(), FileAssocState.UNSUPPORTED)

    def test_a_probe_failure_degrades_to_unsupported_rather_than_raising(self):
        # This runs inside the preferences dialog's status refresh; an exception there would
        # take the whole dialog down.
        self.backend.raise_on_read = True
        self.assertIs(file_assoc.status(), FileAssocState.UNSUPPORTED)

    def test_separator_and_case_differences_are_not_stale(self):
        # A rebuild that normalises path separators must not make a working entry look broken.
        patcher = patch.object(file_assoc, "_expected_command", return_value="C:\\Py\\App.EXE %1")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend.stored = "c:/py/app.exe %1"
        self.assertIs(file_assoc.status(), FileAssocState.REGISTERED)


class TestSetEnabled(unittest.TestCase):
    def setUp(self):
        self.backend = _RecordingBackend()
        patcher = patch.object(file_assoc, "_BACKEND", self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(file_assoc, "_expected_command", return_value="CMD")
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def test_enabling_writes_the_current_command(self):
        ok, _ = file_assoc.set_enabled(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, ["CMD"])

    def test_disabling_writes_none(self):
        file_assoc.set_enabled(True)
        ok, _ = file_assoc.set_enabled(False)
        self.assertTrue(ok)
        self.assertIsNone(self.backend.writes[-1])

    def test_disabling_when_already_off_writes_nothing(self):
        self.backend.stored = None
        ok, _ = file_assoc.set_enabled(False)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [])

    def test_rewriting_a_correct_entry_churns_nothing(self):
        self.backend.stored = "CMD"
        ok, _ = file_assoc.set_enabled(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [])

    def test_a_stale_entry_is_rewritten_rather_than_trusted(self):
        self.backend.stored = "OLD"
        ok, _ = file_assoc.set_enabled(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, ["CMD"])

    def test_turning_off_a_stale_entry_still_removes_it(self):
        # STALE is broken, not "disabled": the requested end state is reached either way.
        self.backend.stored = "OLD"
        ok, _ = file_assoc.set_enabled(False)
        self.assertTrue(ok)
        self.assertIsNone(self.backend.writes[-1])

    def test_an_unsupported_platform_explains_itself_and_writes_nothing(self):
        self.backend.supported = False
        ok, message = file_assoc.set_enabled(True)
        self.assertFalse(ok)
        self.assertIn("Open with", message)
        self.assertEqual(self.backend.writes, [])

    def test_a_backend_exception_is_reported_not_raised(self):
        # It is called from the preferences Save; an exception would abort saving every other
        # setting the user changed in the same visit.
        with patch.object(self.backend, "write", side_effect=RuntimeError("boom")):
            ok, message = file_assoc.set_enabled(True)
        self.assertFalse(ok)
        self.assertIn("boom", message)


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.backend = _RecordingBackend()
        patcher = patch.object(file_assoc, "_BACKEND", self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = patch.object(file_assoc, "_expected_command", return_value="CMD")
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def test_a_user_who_just_unchecked_it_registers_nothing(self):
        # The duty that belongs to the caller: `preferred` must come from a user action, never
        # from a value read at startup. Otherwise merely opening Preferences and pressing Save
        # resurrects an association removed in the system settings.
        self.backend.stored = None
        ok, _ = file_assoc.reconcile(False)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [])

    def test_a_user_who_just_checked_it_registers(self):
        self.backend.stored = None
        file_assoc.reconcile(True)
        self.assertEqual(self.backend.writes, ["CMD"])

    def test_a_stale_entry_is_repaired_on_an_enable(self):
        self.backend.stored = "OLD"
        file_assoc.reconcile(True)
        self.assertEqual(self.backend.writes, ["CMD"])

    def test_reconcile_unchanged_is_a_no_op(self):
        self.backend.stored = "CMD"
        self.backend.default = True
        ok, _ = file_assoc.reconcile(True)
        self.assertTrue(ok)
        self.assertEqual(self.backend.writes, [])


class TestCommandBuilding(unittest.TestCase):
    def test_the_open_command_carries_no_autostart_flag(self):
        # --autostart suppresses the main window whenever a tray exists, so a registered command
        # carrying it would add the file invisibly with no feedback and no window to check.
        rendered = file_assoc._windows_command()
        self.assertNotIn("--autostart", rendered)

    def test_the_open_command_names_the_substitution_slot(self):
        self.assertTrue(file_assoc._windows_command().rstrip().endswith("%1"))

    def test_a_project_path_with_a_space_is_quoted(self):
        with patch.object(file_assoc, "app_command", return_value=[r"C:\My Projects\idm.exe"]):
            rendered = file_assoc._windows_command()
        # Windows parses this with CommandLineToArgvW: an unquoted path with a space becomes two
        # arguments and the file is handed to the wrong program.
        self.assertIn('"C:\\My Projects\\idm.exe"', rendered)

    def test_the_desktop_entry_uses_the_spec_field_code(self):
        with patch.object(file_assoc, "app_command", return_value=["/opt/my idm/my-idm"]):
            exec_line = file_assoc._desktop_exec()
        self.assertIn("%f", exec_line)
        self.assertNotIn("%1", exec_line)

    def test_app_command_is_launch_command_without_the_flag(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", "/opt/my-idm/my-idm"):
            self.assertEqual(launch_command(), ["/opt/my-idm/my-idm", "--autostart"])
            self.assertEqual(app_command(), ["/opt/my-idm/my-idm"])

    def test_a_source_checkout_registers_the_module(self):
        with patch.object(sys, "frozen", False, create=True):
            command = app_command()
        self.assertIn("-m", command)
        self.assertEqual(command[command.index("-m") + 1], "my_idm.main")
        self.assertNotIn("--autostart", command)


class TestWindowsBackend(unittest.TestCase):
    """Exercises the real registry code against a fake ``winreg``, as ``test_autostart`` does."""

    class _Key:
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

        def DeleteKeyEx(self, _root, key):
            if self.fail:
                raise PermissionError("access is denied")
            if key not in self.values:
                raise FileNotFoundError(key)
            del self.values[key]

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
        backend = file_assoc._WindowsBackend()
        patcher2 = patch.object(file_assoc, "_BACKEND", backend)
        patcher2.start()
        self.addCleanup(patcher2.stop)
        self.backend = backend

    def test_registration_writes_the_progid_the_icon_and_the_command(self):
        ok, _ = file_assoc.set_enabled(True)
        self.assertTrue(ok)
        store = self.winreg.values
        self.assertEqual(store[file_assoc._WIN_ASSOC_KEY][""], WINDOWS_PROG_ID)
        self.assertIn(WINDOWS_PROG_ID, store[file_assoc._WIN_ASSOC_KEY]["OpenWithProgids"])
        # The ProgID key's default value is the display name shown in Explorer's Open with list;
        # the identifier lives on the extension key, not repeated here.
        self.assertEqual(
            store[file_assoc._WIN_PROG_KEY][""], file_assoc.WINDOWS_FRIENDLY_NAME
        )
        self.assertTrue(store[rf"{file_assoc._WIN_PROG_KEY}\DefaultIcon"][""])
        self.assertIn("%1", store[file_assoc._WIN_COMMAND_KEY][""])

    def test_a_fresh_registration_is_registered_but_not_default(self):
        file_assoc.set_enabled(True)
        # The normal, permanent Windows state. Reporting it as "on" would be the lie this module
        # exists to avoid.
        self.assertIs(file_assoc.status(), FileAssocState.REGISTERED)

    def test_it_becomes_default_once_the_user_chooses_it(self):
        file_assoc.set_enabled(True)
        self.winreg.values[file_assoc._WIN_USER_CHOICE_KEY] = {"ProgId": WINDOWS_PROG_ID}
        self.assertIs(file_assoc.status(), FileAssocState.DEFAULT)

    def test_another_program_being_the_default_is_not_reported_as_ours(self):
        file_assoc.set_enabled(True)
        self.winreg.values[file_assoc._WIN_USER_CHOICE_KEY] = {"ProgId": "qBittorrent.Association"}
        self.assertIs(file_assoc.status(), FileAssocState.REGISTERED)
        self.assertFalse(file_assoc.is_default())

    def test_removal_deletes_the_progid_tree(self):
        file_assoc.set_enabled(True)
        ok, _ = file_assoc.set_enabled(False)
        self.assertTrue(ok)
        self.assertIs(file_assoc.status(), FileAssocState.DISABLED)
        self.assertNotIn(file_assoc._WIN_COMMAND_KEY, self.winreg.values)

    def test_removal_does_not_steal_the_type_from_another_application(self):
        file_assoc.set_enabled(True)
        # The user switched to another program after we registered. Clearing the extension's
        # default would silently take the type away from them.
        self.winreg.values[file_assoc._WIN_ASSOC_KEY][""] = "qBittorrent.Association"
        file_assoc.set_enabled(False)
        self.assertEqual(
            self.winreg.values[file_assoc._WIN_ASSOC_KEY][""], "qBittorrent.Association"
        )

    def test_removal_clears_our_own_default(self):
        file_assoc.set_enabled(True)
        file_assoc.set_enabled(False)
        self.assertNotIn("", self.winreg.values[file_assoc._WIN_ASSOC_KEY])

    def test_a_locked_hive_is_reported_not_raised(self):
        self.winreg.fail = True
        ok, message = file_assoc.set_enabled(True)
        self.assertFalse(ok)
        self.assertIn("association", message.lower())


class TestLinuxBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patcher_env = patch.dict(
            file_assoc.os.environ, {"XDG_CONFIG_HOME": str(self.tmp)}, clear=False
        )
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        backend = file_assoc._LinuxBackend()
        patcher = patch.object(file_assoc, "_BACKEND", backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        # xdg-mime is not present in a test environment, and calling it is not the point.
        patcher2 = patch.object(file_assoc, "_xdg", return_value=(True, ""))
        patcher2.start()
        self.addCleanup(patcher2.stop)
        self.backend = backend

    def test_the_desktop_entry_declares_the_torrent_mime_type(self):
        file_assoc.set_enabled(True)
        text = file_assoc._linux_entry_path().read_text(encoding="utf-8")
        self.assertIn(f"MimeType={TORRENT_MIME_TYPE};", text)
        self.assertIn("Exec=", text)
        self.assertIn(LINUX_DESKTOP_NAME, file_assoc._linux_entry_path().name)

    def test_xdg_mime_is_asked_to_make_it_the_default(self):
        # Unlike Windows, this is the one platform where the application really may set the
        # default, so it is the one that can honestly report DEFAULT.
        with patch.object(file_assoc, "_xdg", return_value=(True, "")) as xdg:
            file_assoc.set_enabled(True)
        xdg.assert_called_once_with("default", LINUX_DESKTOP_NAME, TORRENT_MIME_TYPE)

    def test_a_current_entry_is_registered(self):
        file_assoc.set_enabled(True)
        self.assertIs(file_assoc.status(), FileAssocState.REGISTERED)

    def test_it_is_default_when_mimeapps_names_it(self):
        file_assoc.set_enabled(True)
        path = file_assoc._linux_mimeapps_path()
        path.write_text(
            "[Default Applications]\n"
            f"{TORRENT_MIME_TYPE}={LINUX_DESKTOP_NAME};\n",
            encoding="utf-8",
        )
        self.assertIs(file_assoc.status(), FileAssocState.DEFAULT)

    def test_another_app_being_the_default_is_not_reported_as_ours(self):
        file_assoc.set_enabled(True)
        file_assoc._linux_mimeapps_path().write_text(
            "[Default Applications]\n"
            f"{TORRENT_MIME_TYPE}=transmission-gtk.desktop;\n",
            encoding="utf-8",
        )
        self.assertIs(file_assoc.status(), FileAssocState.REGISTERED)

    def test_a_default_listing_several_apps_counts_ours_as_default(self):
        file_assoc.set_enabled(True)
        file_assoc._linux_mimeapps_path().write_text(
            "[Default Applications]\n"
            f"{TORRENT_MIME_TYPE}=transmission-gtk.desktop;{LINUX_DESKTOP_NAME};\n",
            encoding="utf-8",
        )
        self.assertIs(file_assoc.status(), FileAssocState.DEFAULT)

    def test_another_section_does_not_confuse_the_parser(self):
        # mimeapps.list has several sections; only [Default Applications] states a default.
        file_assoc.set_enabled(True)
        file_assoc._linux_mimeapps_path().write_text(
            "[Added Associations]\n"
            f"{TORRENT_MIME_TYPE}={LINUX_DESKTOP_NAME};\n",
            encoding="utf-8",
        )
        self.assertIs(file_assoc.status(), FileAssocState.REGISTERED)

    def test_removal_deletes_the_entry_and_our_default(self):
        file_assoc.set_enabled(True)
        file_assoc._linux_mimeapps_path().write_text(
            "[Default Applications]\n"
            f"{TORRENT_MIME_TYPE}={LINUX_DESKTOP_NAME};\n"
            "text/plain=gedit.desktop;\n",
            encoding="utf-8",
        )
        file_assoc.set_enabled(False)
        self.assertFalse(file_assoc._linux_entry_path().exists())
        remaining = file_assoc._linux_mimeapps_path().read_text(encoding="utf-8")
        self.assertNotIn(TORRENT_MIME_TYPE, remaining)
        # Every other type's association survives: a rewritten mimeapps.list that lost them
        # would break the desktop far beyond this feature.
        self.assertIn("text/plain=gedit.desktop;", remaining)

    def test_removal_keeps_another_app_on_the_same_type(self):
        file_assoc.set_enabled(True)
        file_assoc._linux_mimeapps_path().write_text(
            "[Default Applications]\n"
            f"{TORRENT_MIME_TYPE}=transmission-gtk.desktop;{LINUX_DESKTOP_NAME};\n",
            encoding="utf-8",
        )
        file_assoc.set_enabled(False)
        remaining = file_assoc._linux_mimeapps_path().read_text(encoding="utf-8")
        self.assertIn("transmission-gtk.desktop", remaining)
        self.assertNotIn(LINUX_DESKTOP_NAME, remaining)

    def test_a_missing_xdg_mime_helper_explains_the_partial_result(self):
        # The entry is written, so the app is offered for the type; only the default failed. The
        # user has to be told, because double-clicking still opens something else.
        with patch.object(file_assoc, "_xdg", return_value=(False, "")):
            ok, message = file_assoc.set_enabled(True)
        self.assertTrue(ok)
        self.assertIn("Open", message)

    def test_a_command_mismatch_is_stale(self):
        file_assoc.set_enabled(True)
        entry = file_assoc._linux_entry_path()
        lines = entry.read_text(encoding="utf-8").splitlines(keepends=True)
        entry.write_text(
            "".join(
                'Exec="/somewhere/else/other-app %f"\n'
                if line.startswith("Exec=") else line
                for line in lines
            ),
            encoding="utf-8",
        )
        self.assertIs(file_assoc.status(), FileAssocState.STALE)

    def test_a_desktop_entry_mentioning_the_command_only_in_a_comment_is_not_matched(self):
        # Reading the whole file back would let a decorative Comment masquerade as a working
        # entry, so the parser looks for the Exec line specifically.
        file_assoc._linux_entry_path().parent.mkdir(parents=True, exist_ok=True)
        file_assoc._linux_entry_path().write_text(
            f"[Desktop Entry]\nComment={file_assoc._desktop_exec()}\nType=Application\n",
            encoding="utf-8",
        )
        self.assertIsNone(file_assoc._linux_read())


class TestMacOSBackend(unittest.TestCase):
    def setUp(self):
        backend = file_assoc._MacOSBackend()
        patcher = patch.object(file_assoc, "_BACKEND", backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_reports_unsupported(self):
        self.assertIs(file_assoc.status(), FileAssocState.UNSUPPORTED)

    def test_it_explains_why_and_points_at_the_manual_route(self):
        ok, message = file_assoc.set_enabled(True)
        self.assertFalse(ok)
        # LaunchServices reads CFBundleDocumentTypes from the bundle; a non-bundled Python
        # application has nowhere to put it. Saying "unsupported" beats a registration that
        # silently does nothing.
        self.assertIn("bundle", message)
        self.assertIn("Get Info", message)

    def test_the_location_names_the_bundle_plist(self):
        self.assertIn("Info.plist", file_assoc.location())


class TestXdgHelpers(unittest.TestCase):
    def test_a_missing_helper_is_a_quiet_failure(self):
        # A minimal system has no xdg-mime at all; that is not worth an error message.
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(file_assoc._xdg("default", "x.desktop", TORRENT_MIME_TYPE),
                             (False, ""))

    def test_a_non_zero_exit_is_reported_as_a_failure(self):
        with patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 1)
        ):
            self.assertEqual(file_assoc._xdg("default", "x"), (False, ""))

    def test_a_hung_desktop_is_reported_rather_than_blocking_forever(self):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            ok, message = file_assoc._xdg("default", "x")
        self.assertFalse(ok)
        self.assertIn("did not respond", message)


if __name__ == "__main__":
    unittest.main()
