"""Unit tests for toolbar actions, merged Add button, and icon-only Play/Pause buttons."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from PySide6.QtWidgets import QApplication, QToolBar, QToolButton
from PySide6.QtCore import Qt, QSettings

from my_idm.database import Database
from my_idm.main_window import MainWindow
from my_idm.manager import DownloadManager

app = QApplication.instance() or QApplication([])


class TestToolbarLayout(unittest.TestCase):

    def setUp(self):
        QSettings("MyIDM", "My-IDM").clear()
        self.db = Database(":memory:")
        self.db.open()
        self.manager = DownloadManager(self.db)
        self.win = MainWindow(self.manager)

    def tearDown(self):
        self.win.close()
        self.db.close()
        QSettings("MyIDM", "My-IDM").clear()

    def test_merged_add_button_on_toolbar(self):
        """Toolbar should have a single merged Add Download button."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        actions = toolbar.actions()
        # Find action text
        action_texts = [a.text() for a in actions if not a.isSeparator()]

        # "➕ Add Download" should be in the toolbar
        self.assertIn("➕ Add Download", action_texts)
        # Separate "Add Torrent" should NOT be on the toolbar
        self.assertNotIn("📦 Add Torrent", action_texts)
        self.assertNotIn("📦 Add Torrent File…", action_texts)

    def test_play_pause_buttons_icon_only_on_toolbar(self):
        """Play (Resume) and Pause buttons on the toolbar must have ToolButtonIconOnly style."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        btn_resume = toolbar.widgetForAction(self.win._act_resume)
        self.assertIsInstance(btn_resume, QToolButton)
        self.assertEqual(
            btn_resume.toolButtonStyle(),
            Qt.ToolButtonStyle.ToolButtonIconOnly,
            "Play / Resume button must be icon-only on the toolbar"
        )

        btn_pause = toolbar.widgetForAction(self.win._act_pause)
        self.assertIsInstance(btn_pause, QToolButton)
        self.assertEqual(
            btn_pause.toolButtonStyle(),
            Qt.ToolButtonStyle.ToolButtonIconOnly,
            "Pause button must be icon-only on the toolbar"
        )

    def test_play_pause_have_icons_and_tooltips(self):
        """Play and Pause actions have valid QIcons and informative tooltips."""
        self.assertFalse(self.win._act_resume.icon().isNull())
        self.assertFalse(self.win._act_pause.icon().isNull())

        self.assertIn("Resume", self.win._act_resume.toolTip())
        self.assertIn("Pause", self.win._act_pause.toolTip())

    def test_other_toolbar_buttons_retain_text(self):
        """Other buttons like Delete, Move, Recheck retain their text next to icon."""
        toolbar = self.win.findChild(QToolBar)
        btn_delete = toolbar.widgetForAction(self.win._act_delete)
        # Default toolbar style is ToolButtonTextBesideIcon
        self.assertEqual(
            toolbar.toolButtonStyle(),
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )


if __name__ == "__main__":
    unittest.main()
