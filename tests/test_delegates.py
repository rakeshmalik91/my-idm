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
        path = "D:/VeryLongFolderNameThatWillNotFit"
        w = self._w(path) - 60
        out = _shorten_path(path, w, self.fm)
        self.assertNotEqual(out, "D:", out)
        self.assertTrue(out.startswith("D:/…/"), out)

    def test_very_narrow_shows_drive_ellipsis(self):
        path = "D:/Users/Name/Downloads/Movies"
        w = self._w("D:/…") + 5
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