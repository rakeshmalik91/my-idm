"""Unit tests for _shorten_path narrowing behavior using real QFontMetrics."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication

from my_idm.delegates import _shorten_path


_app = QApplication.instance() or QApplication([])


class TestShortenPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        font = QFont("Segoe UI", 10)
        cls.fm = QFontMetrics(font)

    def _w(self, s):
        return self.fm.horizontalAdvance(s)

    def test_full_path_when_it_fits(self):
        path = "D:/Users/Name/Downloads/Movies"
        self.assertEqual(_shorten_path(path, self._w(path), self.fm), path)

    def test_narrow_keeps_leaf(self):
        path = "D:/Users/Name/Downloads/Movies"
        w = self._w(path) - 40
        out = _shorten_path(path, w, self.fm)
        self.assertTrue(out.endswith("/Movies"), out)
        self.assertIn("…", out)
        self.assertNotEqual(out, "D:")

    def test_leaf_too_long_does_not_drop_to_drive(self):
        # Pin the "leaf itself is too long" branch with an absolute pixel budget
        # instead of a font-derived delta. The branch is only reachable when the
        # widest candidate still overflows AND the "D:/…/" prefix leaves at least
        # room for the ellipsis, so the preconditions are asserted explicitly:
        # on a host without Segoe UI (or at a different DPI) a silent shift to
        # the neighbouring branch would otherwise pass unnoticed.
        path = "D:/VeryLongFolderNameThatWillNotFit"
        prefix_w = self._w("D:/…/")
        ellipsis_w = self._w("…")
        w = prefix_w + ellipsis_w + 1
        self.assertLess(
            w, self._w(path), "budget must be too narrow for the full path"
        )
        self.assertLess(
            w,
            self._w("D:/…/VeryLongFolderNameThatWillNotFit"),
            "budget must be too narrow for the leaf-plus-ellipsis candidate",
        )
        out = _shorten_path(path, w, self.fm)
        self.assertNotEqual(out, "D:", out)
        self.assertTrue(out.startswith("D:/…/"), out)
        self.assertLessEqual(
            self._w(out), w, "the elided leaf must still fit the budget"
        )
        self.assertIn("…", out)

    def test_very_narrow_shows_drive_ellipsis(self):
        # The "very narrow" branch needs less room for the leaf than the
        # ellipsis itself takes. The exact width of "D:/…" pins that; the old
        # "+5" fitter inside the leaf-elision branch on a narrow font.
        path = "D:/Users/Name/Downloads/Movies"
        w = self._w("D:/…")
        self.assertLess(
            w - self._w("D:/…/"),
            self._w("…"),
            "no room may be left for the elided leaf",
        )
        out = _shorten_path(path, w, self.fm)
        self.assertEqual(out, "D:/…", out)

    def test_unix_path_keeps_leaf(self):
        path = "/home/user/Downloads/Movies"
        w = self._w(path) - 40
        out = _shorten_path(path, w, self.fm)
        self.assertTrue(out.endswith("/Movies"), out)

    def test_empty(self):
        self.assertEqual(_shorten_path("", 100, self.fm), "")

    def test_root_only(self):
        out = _shorten_path("D:/", 100, self.fm)
        self.assertEqual(out, "D:/")


if __name__ == "__main__":
    unittest.main()