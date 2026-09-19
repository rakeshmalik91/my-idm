"""Tests for duplicate filename auto-numbering."""

import tempfile
import unittest
from pathlib import Path

from my_idm.utils import get_unique_filename


class TestAutoNumbering(unittest.TestCase):
    def test_get_unique_filename_disk_collision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "sample.pdf").touch()

            fn1 = get_unique_filename(tmp, "sample.pdf")
            self.assertEqual(fn1, "sample (1).pdf")

            (tmp / "sample (1).pdf").touch()
            fn2 = get_unique_filename(tmp, "sample.pdf")
            self.assertEqual(fn2, "sample (2).pdf")

    def test_get_unique_filename_reserved_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reserved = {"archive.tar.gz", "archive (1).tar.gz"}

            fn = get_unique_filename(tmp, "archive.tar.gz", reserved_names=reserved)
            self.assertEqual(fn, "archive (2).tar.gz")

    def test_get_unique_filename_no_collision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fn = get_unique_filename(tmp, "newfile.txt")
            self.assertEqual(fn, "newfile.txt")
