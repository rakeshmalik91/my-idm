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


class TestDelegatesDeleting(unittest.TestCase):
    def test_progress_bar_delegate_deleting_renders(self):
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
        from PySide6.QtCore import QRect
        from my_idm.delegates import ProgressBarDelegate
        from my_idm.download_model import DownloadTableModel, Col, DownloadEntry

        delegate = ProgressBarDelegate()
        model = DownloadTableModel()
        e = DownloadEntry(id="d1", url="http://example.com/1.zip", total_size=1000, downloaded_size=500, status="downloading")
        model.load_entries([e])
        model.mark_deleting(["d1"])

        idx = model.index(0, Col.PROGRESS)
        pix = QPixmap(200, 30)
        painter = QPainter(pix)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 200, 30)
        opt.state = QStyle.StateFlag.State_None  # Disabled
        try:
            delegate.paint(painter, opt, idx)
        finally:
            painter.end()

    def test_queue_column_delegate_disabled_text_colour(self):
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
        from my_idm.delegates import QueueColumnDelegate
        from my_idm.styles import Colors

        delegate = QueueColumnDelegate()
        opt = QStyleOptionViewItem()
        opt.state = QStyle.StateFlag.State_None  # Not enabled
        self.assertEqual(delegate._text_colour(opt), QColor(Colors.TEXT_DISABLED))

    def test_download_name_delegate_disabled_paint(self):
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
        from PySide6.QtCore import QRect
        from my_idm.delegates import DownloadNameDelegate
        from my_idm.download_model import DownloadTableModel, Col, DownloadEntry

        delegate = DownloadNameDelegate()
        model = DownloadTableModel()
        e = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="test.zip", status="downloading")
        model.load_entries([e])
        model.mark_deleting(["d1"])

        idx = model.index(0, Col.NAME)
        pix = QPixmap(200, 30)
        painter = QPainter(pix)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 200, 30)
        opt.state = QStyle.StateFlag.State_None
        try:
            delegate.paint(painter, opt, idx)
        finally:
            painter.end()


class TestSectionHeaderDelegate(unittest.TestCase):
    def test_section_header_delegate_paint_select_all_and_clear_selection(self):
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import QStyleOptionViewItem, QTableView
        from PySide6.QtCore import QRect, QItemSelectionModel
        from my_idm.delegates import SectionHeaderDelegate
        from my_idm.download_model import DownloadTableModel, DownloadEntry

        table = QTableView()
        model = DownloadTableModel()
        table.setModel(model)
        model.set_segregated_view(True)

        e1 = DownloadEntry(id="d1", url="http://example.com/1.zip", filename="1.zip", status="downloading")
        e2 = DownloadEntry(id="d2", url="http://example.com/2.zip", filename="2.zip", status="downloading")
        model.load_entries([e1, e2])

        delegate = SectionHeaderDelegate()
        header_idx = model.index(0, 0)
        self.assertTrue(model.is_section_header_row(0))

        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 600, 28)
        opt.widget = table

        pix = QPixmap(600, 28)

        # 1. Initially nothing is selected -> renders without error
        p1 = QPainter(pix)
        try:
            delegate.paint(p1, opt, header_idx)
        finally:
            p1.end()

        # 2. Select all download rows in that section
        download_rows = model.get_section_download_rows("active")
        self.assertEqual(len(download_rows), 2)
        sm = table.selectionModel()
        for r in download_rows:
            sm.select(model.index(r, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)

        # Renders with all rows selected -> "Clear Selection" branch
        p2 = QPainter(pix)
        try:
            delegate.paint(p2, opt, header_idx)
        finally:
            p2.end()

    def test_section_header_delegate_group_progress_bar_geometry(self):
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import QStyleOptionViewItem
        from PySide6.QtCore import QRect
        from unittest.mock import MagicMock
        from my_idm.delegates import SectionHeaderDelegate
        from my_idm.download_model import DownloadEntry

        delegate = SectionHeaderDelegate()
        entry = DownloadEntry(id="sec-act", section_id="active", section_title="Active", section_count=5)
        entry.section_active_count = 2
        entry.section_active_progress = 60.0

        model = MagicMock()
        model.is_section_header_row.return_value = True
        model.get_section_header.return_value = entry

        idx = MagicMock()
        idx.row.return_value = 0
        idx.model.return_value = model
        idx.data.return_value = None

        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 500, 36)

        rounded_rects = []
        pix = QPixmap(500, 36)
        painter = QPainter(pix)
        try:
            orig_draw = painter.drawRoundedRect
            def mock_draw(r, *args):
                rounded_rects.append(QRect(r))
                return orig_draw(r, *args)
            painter.drawRoundedRect = mock_draw
            delegate.paint(painter, opt, idx)
        finally:
            painter.end()

        # Both the progress bar track and the fill should be painted
        self.assertGreaterEqual(len(rounded_rects), 2)
        track_rect = rounded_rects[0]
        self.assertEqual(track_rect.height(), 12)
        # Visual center should be within [17, 21] (centered around 19 for 36px row)
        self.assertAlmostEqual(track_rect.center().y(), 19, delta=2)

    def test_section_header_seeding_without_active_has_no_leading_slash(self):
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import QStyleOptionViewItem
        from PySide6.QtCore import QRect
        from unittest.mock import MagicMock
        from my_idm.delegates import SectionHeaderDelegate
        from my_idm.download_model import DownloadEntry

        delegate = SectionHeaderDelegate()
        entry = DownloadEntry(id="sec-today", section_id="today", section_title="Today", section_count=3)
        entry.section_active_count = 0
        entry.section_seeding_count = 1

        model = MagicMock()
        model.is_section_header_row.return_value = True
        model.get_section_header.return_value = entry

        idx = MagicMock()
        idx.row.return_value = 0
        idx.model.return_value = model
        idx.data.return_value = None

        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 500, 36)

        drawn_texts = []
        pix = QPixmap(500, 36)
        painter = QPainter(pix)
        try:
            orig_draw_text = painter.drawText
            def mock_draw_text(*args):
                # drawText(rect, flags, text)
                for a in args:
                    if isinstance(a, str):
                        drawn_texts.append(a)
                return orig_draw_text(*args)
            painter.drawText = mock_draw_text
            delegate.paint(painter, opt, idx)
        finally:
            painter.end()

        # Drawn texts should be title, " 1 Seeding", " / 3 Total"
        # Specifically, seeding text MUST NOT start with " / "
        self.assertIn(" 1 Seeding", drawn_texts)
        self.assertNotIn(" / 1 Seeding", drawn_texts)
        self.assertIn(" / 3 Total", drawn_texts)

    def test_section_header_seeding_with_active_has_separator(self):
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtWidgets import QStyleOptionViewItem
        from PySide6.QtCore import QRect
        from unittest.mock import MagicMock
        from my_idm.delegates import SectionHeaderDelegate
        from my_idm.download_model import DownloadEntry

        delegate = SectionHeaderDelegate()
        entry = DownloadEntry(id="sec-today", section_id="today", section_title="Today", section_count=3)
        entry.section_active_count = 1
        entry.section_seeding_count = 1

        model = MagicMock()
        model.is_section_header_row.return_value = True
        model.get_section_header.return_value = entry

        idx = MagicMock()
        idx.row.return_value = 0
        idx.model.return_value = model
        idx.data.return_value = None

        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 500, 36)

        drawn_texts = []
        pix = QPixmap(500, 36)
        painter = QPainter(pix)
        try:
            orig_draw_text = painter.drawText
            def mock_draw_text(*args):
                for a in args:
                    if isinstance(a, str):
                        drawn_texts.append(a)
                return orig_draw_text(*args)
            painter.drawText = mock_draw_text
            delegate.paint(painter, opt, idx)
        finally:
            painter.end()

        # Drawn texts should have " 1 Active", " / ", "1 Seeding", " / 3 Total"
        self.assertIn(" 1 Active", drawn_texts)
        self.assertIn(" / ", drawn_texts)
        self.assertIn("1 Seeding", drawn_texts)
        self.assertIn(" / 3 Total", drawn_texts)


if __name__ == "__main__":
    unittest.main()