"""Keep the Linux desktop identity consistent across the three places it is declared.

`StartupWMClass`, `QGuiApplication.setApplicationName()` and `setDesktopFileName()` have to agree
or nothing works, and nothing fails *loudly* when they disagree:

* `StartupWMClass` naming a class the window never publishes means GNOME and KDE cannot associate
  the window with the .desktop entry - so the taskbar entry and the tray icon are not clickable to
  the window, and both match strictly rather than falling back to a guess.
* `setDesktopFileName()` disagreeing with the installed filename means the XDG GlobalShortcuts
  portal rejects the app id, so global hotkeys are unavailable on Wayland.

These are cheap to check and invisible until someone runs the app on a Linux desktop.
"""

import configparser
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "my-idm.desktop"
MAIN = ROOT / "my_idm" / "main.py"
RUN_SH = ROOT / "run.sh"


class TestDesktopEntryIdentity(unittest.TestCase):
    def setUp(self):
        self.desktop_text = DESKTOP.read_text(encoding="utf-8")
        self.main_text = MAIN.read_text(encoding="utf-8")
        self.parser = configparser.RawConfigParser(strict=False)
        # Keys are case-sensitive in the spec; the default lowercasing would hide a typo.
        self.parser.optionxform = str
        self.parser.read_string(self.desktop_text)

    def _entry(self, key):
        return self.parser["Desktop Entry"][key]

    def _from_main(self, call):
        match = re.search(call + r'\("([^"]*)"', self.main_text)
        self.assertIsNotNone(match, f"{call} not found in main.py")
        return match.group(1)

    def test_startup_wm_class_matches_the_application_name(self):
        self.assertEqual(
            self._entry("StartupWMClass"),
            self._from_main("setApplicationName"),
            "GNOME and KDE match the window's WM_CLASS strictly; a mismatch breaks the taskbar "
            "and tray icon association silently",
        )

    def test_desktop_file_name_matches_the_installed_file(self):
        self.assertEqual(
            self._from_main("setDesktopFileName"),
            DESKTOP.name,
            "the app id the GlobalShortcuts portal checks is this string; it has to be the "
            "installed filename",
        )

    def test_required_keys_are_present(self):
        for key in ("Type", "Name", "Exec", "Icon", "Terminal", "Categories"):
            self.assertIn(key, self.parser["Desktop Entry"], f"missing {key}")
        self.assertEqual(self._entry("Type"), "Application")

    def test_terminal_is_false_so_no_console_appears(self):
        """The app is a tray application; a console window on every launch is a regression."""
        self.assertEqual(self._entry("Terminal").lower(), "false")

    def test_exec_actually_exists(self):
        """`TryExec` gates whether the entry appears at all, so it must resolve."""
        tryexec = self._entry("TryExec")
        self.assertTrue(
            (ROOT / tryexec).is_file(),
            f"TryExec={tryexec} does not exist, so no desktop menu would ever list My-IDM",
        )

    def test_exec_and_tryexec_agree(self):
        """A mismatch means the entry is registered against one program and launched as another."""
        self.assertEqual(
            self._entry("Exec").split()[0], self._entry("TryExec").strip()
        )

    def test_categories_use_the_specified_vocabulary(self):
        for category in self._entry("Categories").split(";"):
            if category:
                self.assertRegex(
                    category, r"^[A-Za-z][A-Za-z0-9-]*$",
                    f"{category!r} is not a valid XDG category token",
                )


class TestRunSh(unittest.TestCase):
    def setUp(self):
        self.text = RUN_SH.read_text(encoding="utf-8")
        # Assertions run against the code, not the comments. Several of these strings appear in
        # the header comment precisely because they are what the script does *not* do, so testing
        # the raw text would match its own explanation of itself.
        self.code = "\n".join(
            line for line in self.text.splitlines() if not line.lstrip().startswith("#")
        )

    def test_it_is_executable_or_recently_marked(self):
        """Documented in the repo; git preserves the bit."""
        if not RUN_SH.stat().st_mode & 0o111:
            self.skipTest("run.sh is not marked executable on this filesystem")

    def test_it_uses_the_posix_virtualenv_layout(self):
        self.assertIn(
            "/bin/activate", self.code, "the POSIX venv layout is bin/activate"
        )
        self.assertNotIn(
            "Scripts\\activate", self.code, "the Windows venv layout does not exist on POSIX"
        )

    def test_it_executes_python_so_signals_reach_the_app(self):
        """Without `exec`, Ctrl-C is delivered to the shell and My-IDM keeps running."""
        self.assertRegex(self.code, r"(?m)^exec python3 -m my_idm\.main")

    def test_it_forwards_arguments(self):
        self.assertIn('"$@"', self.code)

    def test_it_sets_no_pythonpath(self):
        """`python -m` already puts the script's directory first.

        Setting PYTHONPATH as well would risk shadowing an installed My-IDM with the checkout.
        """
        self.assertNotIn("PYTHONPATH", self.code)

    def test_it_reports_a_missing_interpreter(self):
        self.assertIn("command -v python3", self.code)


if __name__ == "__main__":
    unittest.main()
