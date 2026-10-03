"""Tests for my_idm/paths.py.

The resolution rules are pure path logic, so they are fully testable on any platform by faking
`sys.platform` and the environment. What genuinely cannot be tested here is the thing that
motivated the module - that a real Linux install honours these locations - which is what the CI
matrix added in Phase 2 is for.

The tests that matter most are the precedence ones. It is easy to write a resolver where the legacy
branch quietly wins everywhere, which would leave every platform on the old path and make the whole
change a no-op that still looks like a change.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from my_idm import paths


class PathResolutionTestCase(unittest.TestCase):
    """Base class giving each test a synthetic HOME/XDG environment."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

        # HOME and APPDATA are read via Path.home() / os.environ, so patch both views.
        env = {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
        }
        for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            env[key] = ""
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        home_patch = patch.object(Path, "home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

        paths.reset_cache()
        self.addCleanup(paths.reset_cache)
        paths.set_data_dir(None)
        self.addCleanup(paths.set_data_dir, None)

    def as_platform(self, name):
        return patch.object(sys, "platform", name)

    def make_legacy(self):
        """Create a pre-existing ~/.my-idm, the thing that must keep winning."""
        legacy = self.home / ".my-idm"
        legacy.mkdir()
        (legacy / "downloads.db").write_bytes(b"")
        return legacy


class TestPlatformLocations(PathResolutionTestCase):
    def test_linux_uses_xdg_data_home(self):
        with self.as_platform("linux"):
            self.assertEqual(
                paths.data_dir(),
                self.home / ".local" / "share" / "my-idm",
            )

    def test_linux_honours_an_absolute_xdg_data_home(self):
        custom = Path(self._tmp.name) / "xdgdata"
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(custom)}):
            with self.as_platform("linux"):
                self.assertEqual(paths.data_dir(), custom / "my-idm")

    def test_a_relative_xdg_data_home_is_ignored(self):
        """The XDG spec calls a relative base directory invalid.

        Honouring one would scatter application data somewhere no desktop environment looks, and
        the resulting folder would be invisible to the user's own file manager.
        """
        with patch.dict(os.environ, {"XDG_DATA_HOME": "relative/data"}):
            with self.as_platform("linux"):
                self.assertEqual(
                    paths.data_dir(),
                    self.home / ".local" / "share" / "my-idm",
                )

    def test_macos_uses_application_support(self):
        with self.as_platform("darwin"):
            self.assertEqual(
                paths.data_dir(),
                self.home / "Library" / "Application Support" / "My-IDM",
            )

    def test_windows_uses_appdata(self):
        with self.as_platform("win32"):
            self.assertEqual(
                paths.data_dir(),
                self.home / "AppData" / "Roaming" / "My-IDM",
            )

    def test_config_dir_is_separate_from_data_on_linux(self):
        """Config and data must not share a directory.

        A stray file written into the data directory ends up in backups of the user's download
        history, which is not something a config file should be able to do.
        """
        with self.as_platform("linux"):
            self.assertEqual(paths.config_dir(), self.home / ".config" / "my-idm")
            self.assertNotEqual(paths.config_dir(), paths.data_dir())

    def test_config_dir_honours_xdg_config_home(self):
        custom = Path(self._tmp.name) / "xdgcfg"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(custom)}):
            with self.as_platform("linux"):
                self.assertEqual(paths.config_dir(), custom / "my-idm")


class TestLegacyFallback(PathResolutionTestCase):
    """The rule that stops an existing install from losing its download history."""

    def test_an_existing_legacy_dir_wins_on_every_platform(self):
        legacy = self.make_legacy()
        for platform in ("win32", "linux", "darwin"):
            with self.subTest(platform=platform):
                paths.reset_cache()
                with self.as_platform(platform):
                    self.assertEqual(
                        paths.data_dir(),
                        legacy,
                        "an existing profile must not move, or the user's history is orphaned",
                    )

    def test_without_a_legacy_dir_each_platform_gets_its_own_location(self):
        expected = {
            "linux": self.home / ".local" / "share" / "my-idm",
            "darwin": self.home / "Library" / "Application Support" / "My-IDM",
            "win32": self.home / "AppData" / "Roaming" / "My-IDM",
        }
        for platform, want in expected.items():
            with self.subTest(platform=platform):
                paths.reset_cache()
                with self.as_platform(platform):
                    self.assertEqual(paths.data_dir(), want)

    def test_a_file_at_the_legacy_path_is_not_a_profile(self):
        """`~/.my-idm` as a *file* must not be adopted as a directory.

        Every write would then fail with a permission error, at startup, with a message about
        My-IDM's data directory rather than about the stray file.
        """
        (self.home / ".my-idm").write_text("not a profile")
        with self.as_platform("linux"):
            self.assertEqual(
                paths.data_dir(),
                self.home / ".local" / "share" / "my-idm",
            )

    def test_an_empty_legacy_dir_still_counts_as_a_profile(self):
        """Empty is not absent.

        The directory existing at all is the signal - it means this app created it. Judging by
        emptiness would move a profile whose downloads happen to have been moved out, which is the
        one moment a user is least likely to notice.
        """
        (self.home / ".my-idm").mkdir()
        with self.as_platform("linux"):
            self.assertEqual(paths.data_dir(), self.home / ".my-idm")

    def test_the_override_beats_the_legacy_dir(self):
        """Tests and `--data-dir` must never touch the real profile."""
        legacy = self.make_legacy()
        elsewhere = Path(self._tmp.name) / "elsewhere"
        paths.set_data_dir(elsewhere)
        with self.as_platform("linux"):
            self.assertEqual(paths.data_dir(), elsewhere)
        self.assertTrue(legacy.is_dir(), "the legacy profile must be left untouched")


class TestDerivedPaths(PathResolutionTestCase):
    def test_every_derived_path_sits_under_the_data_dir(self):
        """One directory means one place to back up, and no path can escape it."""
        with self.as_platform("linux"):
            base = paths.data_dir()
            derived = {
                "database": paths.database_path(),
                "fastresume": paths.fastresume_dir(),
                "tor": paths.tor_data_dir(),
                "logs": paths.logs_dir(),
                "backlog": paths.backlog_path(),
            }
            for name, path in derived.items():
                with self.subTest(path=name):
                    self.assertEqual(path.parent, base)

    def test_the_database_is_named_downloads_db(self):
        with self.as_platform("linux"):
            self.assertEqual(paths.database_path().name, "downloads.db")

    def test_paths_are_cached_and_stable(self):
        with self.as_platform("linux"):
            first = paths.data_dir()
            self.assertIs(paths.data_dir(), first)

    def test_reset_cache_picks_up_an_environment_change(self):
        with self.as_platform("linux"):
            before = paths.data_dir()
        paths.reset_cache()
        custom = Path(self._tmp.name) / "later"
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(custom)}):
            with self.as_platform("linux"):
                self.assertNotEqual(paths.data_dir(), before)

    def test_ensure_data_dir_creates_it(self):
        with self.as_platform("linux"):
            target = paths.data_dir()
            self.assertFalse(target.exists())
            self.assertEqual(paths.ensure_data_dir(), target)
            self.assertTrue(target.is_dir())

    def test_ensure_data_dir_reports_an_unwritable_location(self):
        """The failure has to name the directory and the escape hatch.

        A bare OSError here surfaces as a stack trace during startup with no indication of which
        path was at fault or what to do about it.
        """
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("a file, not a directory")
        paths.set_data_dir(blocker / "nested")
        with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            with self.assertRaises(RuntimeError) as ctx:
                paths.ensure_data_dir()
        message = str(ctx.exception)
        self.assertIn("MYIDM_DATA_DIR", message)


class TestDownloadsDir(PathResolutionTestCase):
    """`Downloads` is special: it is a *user-content* location, not application data."""

    def setUp(self):
        super().setUp()
        try:
            from PySide6.QtCore import QStandardPaths
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"Qt unavailable: {exc}")
        self._sp = QStandardPaths

    def test_it_prefers_qstandardpaths_over_a_hardcoded_name(self):
        """The point of using QStandardPaths at all.

        `~/Downloads` is wrong on any install that localises directory names - `~/Загрузки`,
        `~/Descargas` - and using it would silently create a second folder beside the user's real
        one.
        """
        resolved = Path(self._tmp.name) / "localised-downloads-dir"
        paths.reset_cache()
        with patch.object(self._sp, "writableLocation", return_value=str(resolved)):
            self.assertEqual(paths.default_downloads_dir(), resolved)

    def test_it_falls_back_when_qt_refuses(self):
        """Must work before a QApplication exists.

        The manager resolves the default target during construction, which in tests and on
        `--help` paths can happen before Qt is up - and an exception here would propagate into
        startup rather than degrading.
        """
        paths.reset_cache()
        with patch.object(self._sp, "writableLocation", side_effect=RuntimeError("no app")):
            with self.as_platform("linux"):
                self.assertEqual(paths.default_downloads_dir(), self.home / "Downloads")

    def test_an_empty_qstandardpaths_answer_falls_back(self):
        """Qt can return an empty string rather than raising; that is not a usable location."""
        paths.reset_cache()
        with patch.object(self._sp, "writableLocation", return_value=""):
            with self.as_platform("linux"):
                self.assertEqual(paths.default_downloads_dir(), self.home / "Downloads")


if __name__ == "__main__":
    unittest.main()
