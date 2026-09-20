"""Unit tests for IDMSplashScreen and startup splash integration."""

import unittest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow

from my_idm.main import parse_args
from my_idm.splash import IDMSplashScreen, SPLASH_WIDTH, SPLASH_HEIGHT

app = QApplication.instance() or QApplication([])


class TestIDMSplashScreen(unittest.TestCase):
    """Tests for IDMSplashScreen rendering, progress updating, and lifecycle."""

    def test_splash_screen_initial_properties(self):
        splash = IDMSplashScreen()
        try:
            self.assertEqual(splash.width(), SPLASH_WIDTH)
            self.assertEqual(splash.height(), SPLASH_HEIGHT)
            self.assertEqual(splash.progress, 0)
            self.assertIn("Starting", splash.message)
            self.assertTrue(splash.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
            flags = splash.windowFlags()
            self.assertTrue(bool(flags & Qt.WindowType.SplashScreen))
            self.assertTrue(bool(flags & Qt.WindowType.FramelessWindowHint))
        finally:
            splash.close()

    def test_splash_screen_message_and_progress_updating(self):
        splash = IDMSplashScreen()
        try:
            splash.set_message("Loading database...", 30)
            self.assertEqual(splash.message, "Loading database...")
            self.assertEqual(splash.progress, 30)

            splash.set_message("Almost ready...", 85)
            self.assertEqual(splash.message, "Almost ready...")
            self.assertEqual(splash.progress, 85)

            # Test progress clamping (under 0 and over 100)
            splash.set_progress(-20)
            self.assertEqual(splash.progress, 0)

            splash.set_progress(150)
            self.assertEqual(splash.progress, 100)
        finally:
            splash.close()

    def test_splash_screen_paint_rendering(self):
        splash = IDMSplashScreen()
        try:
            splash.set_message("Rendering test...", 50)
            pix = QPixmap(SPLASH_WIDTH, SPLASH_HEIGHT)
            splash.render(pix)
            self.assertFalse(pix.isNull())
        finally:
            splash.close()

    def test_splash_screen_finish_with_window(self):
        splash = IDMSplashScreen()
        win = QMainWindow()
        try:
            splash.show()
            win.show()
            splash.finish(win)
        finally:
            splash.close()
            win.close()

    def test_parse_args_no_splash_flag(self):
        import sys
        from unittest.mock import patch

        with patch.object(sys, "argv", ["my-idm", "--no-splash"]):
            args = parse_args()
            self.assertTrue(args.no_splash)

        with patch.object(sys, "argv", ["my-idm"]):
            args = parse_args()
            self.assertFalse(args.no_splash)


class TestIDMExitSplashScreen(unittest.TestCase):
    """Tests for IDMExitSplashScreen properties, painting, and manager shutdown integration."""

    def test_exit_splash_initial_properties(self):
        from my_idm.splash import IDMExitSplashScreen, EXIT_SPLASH_WIDTH, EXIT_SPLASH_HEIGHT
        splash = IDMExitSplashScreen()
        try:
            self.assertEqual(splash.width(), EXIT_SPLASH_WIDTH)
            self.assertEqual(splash.height(), EXIT_SPLASH_HEIGHT)
            self.assertEqual(splash.progress, 0)
            self.assertIn("Closing", splash.message)
            self.assertTrue(splash.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
            flags = splash.windowFlags()
            self.assertTrue(bool(flags & Qt.WindowType.SplashScreen))
            self.assertTrue(bool(flags & Qt.WindowType.FramelessWindowHint))
        finally:
            splash.close()

    def test_exit_splash_message_and_progress_updating(self):
        from my_idm.splash import IDMExitSplashScreen
        splash = IDMExitSplashScreen()
        try:
            splash.set_message("Saving torrent fastresume...", 50)
            self.assertEqual(splash.message, "Saving torrent fastresume...")
            self.assertEqual(splash.progress, 50)

            splash.set_progress(120)
            self.assertEqual(splash.progress, 100)

            splash.set_progress(-10)
            self.assertEqual(splash.progress, 0)
        finally:
            splash.close()

    def test_exit_splash_paint_rendering(self):
        from my_idm.splash import IDMExitSplashScreen, EXIT_SPLASH_WIDTH, EXIT_SPLASH_HEIGHT
        splash = IDMExitSplashScreen()
        try:
            splash.set_message("Rendering exit splash test...", 65)
            pix = QPixmap(EXIT_SPLASH_WIDTH, EXIT_SPLASH_HEIGHT)
            splash.render(pix)
            self.assertFalse(pix.isNull())
        finally:
            splash.close()

    def test_manager_stop_reports_progress(self):
        from my_idm.database import Database
        from my_idm.manager import DownloadManager
        db = Database(":memory:")
        db.open()
        manager = DownloadManager(db)
        manager.start()

        reports = []
        def on_status(msg, pct):
            reports.append((msg, pct))

        manager.stop(status_cb=on_status)
        db.close()

        self.assertGreater(len(reports), 0)
        self.assertIn("Shutdown complete.", reports[-1][0])
        self.assertEqual(reports[-1][1], 100)

    def test_main_window_close_with_exit_splash_flag(self):
        from my_idm.database import Database
        from my_idm.manager import DownloadManager
        from my_idm.main_window import MainWindow

        db = Database(":memory:")
        db.open()
        manager = DownloadManager(db)
        win = MainWindow(manager, show_exit_splash=True)
        try:
            win.show()
            win.close()
            self.assertFalse(win.isVisible())
        finally:
            manager.stop()
            db.close()


if __name__ == "__main__":
    unittest.main()
