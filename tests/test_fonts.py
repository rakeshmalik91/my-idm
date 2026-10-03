"""Tests for my_idm/fonts.py.

The lists themselves are data, so what matters is that the *dispatch* is right: the platform key
must select the intended chain, the chains must never come back empty, and the QFont constructors
must survive a font database that cannot answer.

The empty-list guard is the load-bearing test. A resolver that returned `[]` would not raise - Qt
would silently substitute its own default, and the app would look subtly wrong on exactly the
platforms this work exists to fix, with nothing in the log.
"""

import unittest
from unittest.mock import patch

from my_idm import fonts
from PySide6.QtGui import QFont, QFontDatabase


class TestPlatformDispatch(unittest.TestCase):
    def test_windows_gets_segoe_ui_first(self):
        """Must not change how the app looks for existing Windows users."""
        with patch.object(fonts.sys, "platform", "win32"):
            families = fonts.ui_font_families()
        self.assertEqual(families[0], "Segoe UI")

    def test_macos_gets_the_system_face_first(self):
        with patch.object(fonts.sys, "platform", "darwin"):
            families = fonts.ui_font_families()
        self.assertIn(families[0], (".AppleSystemUIFont", "SF Pro Text"))

    def test_linux_gets_a_linux_default_first(self):
        with patch.object(fonts.sys, "platform", "linux"):
            families = fonts.ui_font_families()
        self.assertIn(families[0], ("Inter", "Noto Sans", "Ubuntu", "Cantarell"))

    def test_an_unknown_platform_falls_back_rather_than_raising(self):
        with patch.object(fonts.sys, "platform", "freebsd14"):
            families = fonts.ui_font_families()
        self.assertTrue(families, "an unknown platform must still get a usable list")

    def test_no_platform_yields_an_empty_list(self):
        """The failure this guards.

        An empty list does not raise: Qt substitutes its own default silently. The app then looks
        subtly wrong on the platforms this module exists to fix, with nothing in the log.
        """
        for platform in ("win32", "darwin", "linux", "freebsd14", ""):
            with self.subTest(platform=platform):
                with patch.object(fonts.sys, "platform", platform):
                    self.assertTrue(fonts.ui_font_families())
                    self.assertTrue(fonts.mono_font_families())
                    self.assertTrue(fonts.emoji_font_families())


class TestMonospace(unittest.TestCase):
    def test_monospace_differs_from_the_ui_chain(self):
        """Sharing a chain would defeat the purpose.

        `shorten_path` and the segment table measure text to decide where to elide; a proportional
        substitution makes those measurements wrong and columns ragged.
        """
        for platform in ("win32", "darwin", "linux"):
            with self.subTest(platform=platform):
                with patch.object(fonts.sys, "platform", platform):
                    self.assertNotEqual(
                        fonts.mono_font_families(), fonts.ui_font_families()
                    )

    def test_no_family_is_shared_between_ui_and_mono(self):
        for platform in ("win32", "darwin", "linux"):
            with self.subTest(platform=platform):
                with patch.object(fonts.sys, "platform", platform):
                    overlap = set(fonts.ui_font_families()) & set(fonts.mono_font_families())
                    self.assertFalse(
                        overlap, f"{overlap} is not fixed-pitch and must not be offered"
                    )


class TestFontConstruction(unittest.TestCase):
    def test_ui_font_builds_and_honours_bold(self):
        with patch.object(fonts.sys, "platform", "linux"):
            plain = fonts.ui_font(11)
            bold = fonts.ui_font(11, bold=True)
        self.assertIsInstance(plain, QFont)
        self.assertEqual(plain.pointSize(), 11)
        self.assertEqual(bold.weight(), QFont.Weight.Bold)
        self.assertNotEqual(plain.weight(), QFont.Weight.Bold)

    def test_mono_font_builds(self):
        with patch.object(fonts.sys, "platform", "darwin"):
            font = fonts.mono_font(10)
        self.assertIsInstance(font, QFont)
        self.assertEqual(font.pointSize(), 10)

    def test_emoji_font_builds(self):
        self.assertIsInstance(fonts.emoji_font(14), QFont)

    def test_an_emptied_table_still_yields_a_legible_font(self):
        """The guard that actually protects here.

        An empty family list does not raise - Qt silently substitutes its own default, so the app
        looks wrong in a way nothing in the log would explain. Patching a table to empty proves the
        `or _GENERIC_SANS` fallback runs.
        """
        empty = {"win32": [], "darwin": [], "default": []}
        with patch.dict(fonts._UI_FAMILIES, empty, clear=True):
            with patch.object(fonts.sys, "platform", "linux"):
                font = fonts.ui_font(10)
                self.assertIsInstance(font, QFont)
                self.assertTrue(font.family(), "a font must always end up with some family")

    def test_an_emptied_mono_table_still_yields_a_font(self):
        with patch.dict(fonts._MONO_FAMILIES, {"win32": [], "darwin": [], "default": []}, clear=True):
            with patch.object(fonts.sys, "platform", "linux"):
                font = fonts.mono_font(10)
                self.assertIsInstance(font, QFont)
                self.assertTrue(font.family())


class TestAvailabilityProbe(unittest.TestCase):
    def test_it_reports_the_first_family_that_exists(self):
        with patch.object(fonts.QFontDatabase, "families", staticmethod(lambda: ["DejaVu Sans"])):
            self.assertEqual(
                fonts.available_family(["Noto Sans", "DejaVu Sans"]), "DejaVu Sans"
            )

    def test_it_returns_none_when_nothing_matches(self):
        with patch.object(fonts.QFontDatabase, "families", staticmethod(lambda: ["Arial"])):
            self.assertIsNone(fonts.available_family(["Noto Sans", "DejaVu Sans"]))

    def test_a_missing_font_database_is_not_an_error(self):
        """Probed before a QGuiApplication exists in some startup orders."""
        with patch.object(
            fonts.QFontDatabase, "families", side_effect=RuntimeError("no app")
        ):
            self.assertIsNone(fonts.available_family(["Noto Sans"]))


class TestStylesheet(unittest.TestCase):
    def test_the_chain_is_comma_separated_and_quoted(self):
        with patch.object(fonts.sys, "platform", "linux"):
            expected = fonts.ui_font_families()
            value = fonts.stylesheet_family()
        self.assertEqual(
            [part.strip().strip('"') for part in value.split(",")],
            expected,
        )

    def test_every_family_is_quoted(self):
        """An unquoted family with a space or a digit is not a valid CSS identifier."""
        for part in fonts.stylesheet_family().split(","):
            self.assertTrue(part.strip().startswith('"'), f"unquoted: {part!r}")
            self.assertTrue(part.strip().endswith('"'), f"unquoted: {part!r}")

    def test_no_trailing_comma_which_would_produce_an_empty_selector(self):
        self.assertFalse(fonts.stylesheet_family().rstrip().endswith(","))


if __name__ == "__main__":
    unittest.main()
