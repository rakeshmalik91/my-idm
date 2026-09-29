"""Unit tests for IDMSplashScreen and startup splash integration."""

import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow

from my_idm.config import GeneralConfig
from my_idm.main import parse_args
from my_idm.splash import (
    EXIT_SPLASH_HEIGHT,
    EXIT_SPLASH_WIDTH,
    IDMExitSplashScreen,
    IDMSplashScreen,
    SPLASH_HEIGHT,
    SPLASH_WIDTH,
)

app = QApplication.instance() or QApplication([])

#: A colour the splash paint routines never produce. Prefilling the render target
#: with it turns "was anything painted?" into a real question: a freshly built
#: QPixmap is never null, so `assertFalse(pix.isNull())` proves nothing.
SENTINEL = QColor(1, 254, 7)


def render_splash(splash, width, height, progress=None):
    """Render ``splash`` over a sentinel-filled pixmap and return the QImage."""
    if progress is not None:
        splash.set_progress(progress)
    pix = QPixmap(width, height)
    pix.fill(SENTINEL)
    splash.render(pix)
    return pix.toImage()


def count_non_sentinel_pixels(image, width, height, step=4):
    """Count sampled pixels the splash actually painted."""
    count = 0
    for y in range(0, height, step):
        for x in range(0, width, step):
            if image.pixelColor(x, y) != SENTINEL:
                count += 1
    return count


class _RecordingExitSplash:
    """Stand-in for ``IDMExitSplashScreen`` used to observe the shutdown sequence.

    The real splash is a top-level widget; instantiating it for real would flash a
    window on the developer's desktop on every test run. This records the calls
    ``MainWindow.closeEvent`` makes so the exit-splash contract can be asserted
    without a window ever existing.
    """

    instances: list["_RecordingExitSplash"] = []

    def __init__(self):
        self.messages: list[tuple] = []
        self.shown = False
        self.closed = False
        self.position = None
        _RecordingExitSplash.instances.append(self)

    def width(self):
        return EXIT_SPLASH_WIDTH

    def height(self):
        return EXIT_SPLASH_HEIGHT

    def move(self, x, y):
        self.position = (x, y)

    def show(self):
        self.shown = True

    def set_message(self, message, progress=None):
        self.messages.append((message, progress))

    def close(self):
        self.closed = True


def destroy_window(win, manager, db):
    """Tear a MainWindow down for real.

    ``MainWindow.close()`` is a no-op while close-to-tray is configured: it just
    hides the window. The ``_details_timer`` (1 s) and ``_tor_availability_timer``
    (4 s, opens a real socket probe) then keep running for the rest of the
    session, so the window must be force-destroyed instead.
    """
    win._force_exit = True
    # Teardown must never flash the real exit splash.
    win._show_exit_splash = False
    win.close()
    win.deleteLater()
    QApplication.processEvents()
    manager.stop()
    db.close()


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
            splash.deleteLater()
            QApplication.processEvents()

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
            self.assertEqual(
                splash.message,
                "Almost ready...",
                "set_progress() must not touch the status message",
            )

            splash.set_progress(150)
            self.assertEqual(splash.progress, 100)
        finally:
            splash.close()
            splash.deleteLater()
            QApplication.processEvents()

    def test_set_message_with_no_progress_keeps_progress(self):
        splash = IDMSplashScreen()
        try:
            splash.set_progress(42)
            splash.set_message("Copying library resources...")
            self.assertEqual(splash.message, "Copying library resources...")
            self.assertEqual(
                splash.progress,
                42,
                "set_message() without a progress value must leave the bar untouched",
            )
        finally:
            splash.close()
            splash.deleteLater()
            QApplication.processEvents()

    def test_splash_screen_paint_rendering(self):
        splash = IDMSplashScreen()
        try:
            splash.set_message("Rendering test...", 50)

            blank = QPixmap(SPLASH_WIDTH, SPLASH_HEIGHT)
            blank.fill(SENTINEL)
            self.assertEqual(
                blank.toImage().pixelColor(0, 0),
                SENTINEL,
                "sanity check: an unfilled render target is pure sentinel",
            )

            image = render_splash(splash, SPLASH_WIDTH, SPLASH_HEIGHT)

            self.assertFalse(image.isNull())
            # A rounded card is painted: the centre of the card is not the sentinel.
            self.assertNotEqual(
                image.pixelColor(SPLASH_WIDTH // 2, 60),
                SENTINEL,
                "the splash body was never painted; paintEvent() did not run",
            )
            # ...and the rounded corners are deliberately left untouched.
            self.assertEqual(
                image.pixelColor(0, 0),
                SENTINEL,
                "the top-left corner must stay transparent (rounded card), so the "
                "paint routine must clip rather than fill the whole rect",
            )
            painted = count_non_sentinel_pixels(image, SPLASH_WIDTH, SPLASH_HEIGHT)
            self.assertGreater(
                painted,
                1000,
                f"only {painted} sampled pixels were painted; the splash is "
                "essentially blank",
            )
        finally:
            splash.close()
            splash.deleteLater()
            QApplication.processEvents()

    def test_splash_screen_progress_bar_is_actually_drawn(self):
        """The progress fill must change real pixels, not just the attribute."""
        splash = IDMSplashScreen()
        try:
            at_zero = render_splash(splash, SPLASH_WIDTH, SPLASH_HEIGHT, progress=0)
            at_full = render_splash(splash, SPLASH_WIDTH, SPLASH_HEIGHT, progress=100)
            at_half = render_splash(splash, SPLASH_WIDTH, SPLASH_HEIGHT, progress=50)

            # Progress bar geometry from IDMSplashScreen.paintEvent:
            # x = 36 .. width-36, y = 184 .. 190, height 6.
            bar_y = 187
            left_x = 40
            right_x = SPLASH_WIDTH - 40
            track = at_zero.pixelColor(left_x, bar_y)

            self.assertEqual(
                track,
                at_zero.pixelColor(right_x, bar_y),
                "an empty progress bar must render a flat unfilled track",
            )
            self.assertNotEqual(
                track,
                at_half.pixelColor(left_x, bar_y),
                "a 50% progress bar must paint a fill over the left end of the track",
            )
            self.assertEqual(
                at_half.pixelColor(right_x, bar_y),
                track,
                "at 50% the right half of the bar must still be bare track",
            )
            self.assertNotEqual(
                track,
                at_full.pixelColor(right_x, bar_y),
                "a 100% progress bar must reach the right edge of the track",
            )
            self.assertNotEqual(
                at_half.pixelColor(left_x, bar_y),
                at_full.pixelColor(right_x, bar_y),
                "the fill must follow the ACCENT -> CYAN -> GREEN gradient",
            )
        finally:
            splash.close()
            splash.deleteLater()
            QApplication.processEvents()

    def test_splash_screen_finish_with_window(self):
        """finish() takes the splash down and must not conjure a main window."""
        splash = IDMSplashScreen()
        win = QMainWindow()
        try:
            self.assertFalse(
                splash.isVisible(),
                "a freshly constructed splash must not be visible before finish()",
            )

            splash.finish(win)

            self.assertFalse(
                splash.isVisible(),
                "finish() must close the splash so the next window is unobstructed",
            )
            self.assertFalse(
                splash.testAttribute(Qt.WidgetAttribute.WA_WState_Visible),
                "finish() must clear the visible widget state, not merely repaint",
            )
            self.assertFalse(
                win.isVisible(),
                "finish() must not show a main window that was never shown",
            )
        finally:
            splash.close()
            splash.deleteLater()
            win.close()
            win.deleteLater()
            QApplication.processEvents()

    def test_splash_screen_finish_centers_over_a_native_main_window(self):
        """With a real (shown) main window, finish() hides the splash and keeps the window up."""
        splash = IDMSplashScreen()
        win = QMainWindow()
        try:
            win.show()
            QApplication.processEvents()
            self.assertFalse(splash.isVisible())

            splash.finish(win)

            self.assertTrue(
                win.isVisible(),
                "finish() must leave the main window on screen",
            )
            self.assertFalse(
                splash.isVisible(),
                "finish() must take the splash down once the main window is up",
            )
        finally:
            win.close()
            win.deleteLater()
            splash.close()
            splash.deleteLater()
            QApplication.processEvents()

    def test_parse_args_no_splash_flag(self):
        import sys

        with mock.patch.object(sys, "argv", ["my-idm", "--no-splash"]):
            args = parse_args()
            self.assertTrue(args.no_splash)

        with mock.patch.object(sys, "argv", ["my-idm"]):
            args = parse_args()
            self.assertFalse(args.no_splash)


class TestIDMExitSplashScreen(unittest.TestCase):
    """Tests for IDMExitSplashScreen properties, painting, and manager shutdown integration."""

    def test_exit_splash_initial_properties(self):
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
            splash.deleteLater()
            QApplication.processEvents()

    def test_exit_splash_message_and_progress_updating(self):
        splash = IDMExitSplashScreen()
        try:
            splash.set_message("Saving torrent fastresume...", 50)
            self.assertEqual(splash.message, "Saving torrent fastresume...")
            self.assertEqual(splash.progress, 50)

            splash.set_progress(120)
            self.assertEqual(splash.progress, 100)
            self.assertEqual(
                splash.message,
                "Saving torrent fastresume...",
                "set_progress() must not overwrite the shutdown step text",
            )

            splash.set_progress(-10)
            self.assertEqual(splash.progress, 0)
        finally:
            splash.close()
            splash.deleteLater()
            QApplication.processEvents()

    def test_exit_splash_paint_rendering(self):
        splash = IDMExitSplashScreen()
        try:
            splash.set_message("Rendering exit splash test...", 65)

            image = render_splash(splash, EXIT_SPLASH_WIDTH, EXIT_SPLASH_HEIGHT)

            self.assertFalse(image.isNull())
            self.assertNotEqual(
                image.pixelColor(EXIT_SPLASH_WIDTH // 2, 55),
                SENTINEL,
                "the exit splash body was never painted; paintEvent() did not run",
            )
            self.assertEqual(
                image.pixelColor(0, 0),
                SENTINEL,
                "the exit splash corner must stay transparent (rounded card)",
            )
            painted = count_non_sentinel_pixels(image, EXIT_SPLASH_WIDTH, EXIT_SPLASH_HEIGHT)
            self.assertGreater(
                painted,
                1000,
                f"only {painted} sampled pixels were painted by the exit splash",
            )
        finally:
            splash.close()
            splash.deleteLater()
            QApplication.processEvents()

    def test_exit_splash_progress_bar_is_actually_drawn(self):
        splash = IDMExitSplashScreen()
        try:
            at_zero = render_splash(splash, EXIT_SPLASH_WIDTH, EXIT_SPLASH_HEIGHT, progress=0)
            at_full = render_splash(splash, EXIT_SPLASH_WIDTH, EXIT_SPLASH_HEIGHT, progress=100)

            # Exit splash bar geometry: x = 36 .. width-36, y = 156 .. 162.
            bar_y = 159
            left_x = 40
            self.assertNotEqual(
                at_zero.pixelColor(left_x, bar_y),
                at_full.pixelColor(left_x, bar_y),
                "a 100% shutdown progress bar must paint a fill over the track",
            )
        finally:
            splash.close()
            splash.deleteLater()
            QApplication.processEvents()

    def test_manager_stop_reports_progress(self):
        from my_idm.database import Database
        from my_idm.manager import DownloadManager

        db = Database(":memory:")
        db.open()
        manager = DownloadManager(db)
        try:
            manager.start()

            reports = []

            def on_status(msg, pct):
                reports.append((msg, pct))

            manager.stop(status_cb=on_status)
            db.close()
            db = None

            self.assertGreater(len(reports), 0, "manager.stop() must report shutdown progress")
            self.assertIn("Shutdown complete.", reports[-1][0])
            self.assertEqual(reports[-1][1], 100)

            percentages = [pct for _, pct in reports]
            self.assertEqual(
                percentages,
                sorted(percentages),
                f"shutdown progress must advance monotonically, got {percentages!r}",
            )
            self.assertEqual(
                len(set(percentages)),
                len(percentages),
                f"each shutdown step must report a distinct percentage, got {percentages!r}",
            )
            self.assertTrue(
                all(isinstance(pct, int) and 0 <= pct <= 100 for pct in percentages),
                f"shutdown percentages must be ints in 0..100, got {percentages!r}",
            )
        finally:
            if manager._stopped is not True:
                manager.stop()
            if db is not None:
                db.close()

    def test_main_window_close_with_exit_splash_flag(self):
        from my_idm.database import Database
        from my_idm.manager import DownloadManager
        from my_idm.main_window import MainWindow

        db = Database(":memory:")
        db.open()
        manager = DownloadManager(db)
        # close_to_tray / enable_system_tray are the *default* config, and with
        # them on, closeEvent returns early and the exit-splash branch never runs.
        manager.set_general_config(
            GeneralConfig(close_to_tray=False, enable_system_tray=False)
        )
        self.assertFalse(manager.general_config.close_to_tray)
        self.assertFalse(manager.general_config.enable_system_tray)

        win = MainWindow(manager, show_exit_splash=True)
        _RecordingExitSplash.instances.clear()
        try:
            with mock.patch(
                "my_idm.splash.IDMExitSplashScreen", _RecordingExitSplash
            ):
                win.show()
                QApplication.processEvents()
                self.assertTrue(win.isVisible())

                win.close()
                QApplication.processEvents()

            self.assertEqual(
                len(_RecordingExitSplash.instances),
                1,
                "the exit-splash branch of closeEvent must construct exactly one "
                f"exit splash, got {len(_RecordingExitSplash.instances)}",
            )
            splash = _RecordingExitSplash.instances[0]
            self.assertTrue(splash.shown, "the exit splash must actually be shown")
            self.assertIsNotNone(splash.position, "the exit splash must be positioned")
            self.assertEqual(
                splash.messages[0],
                ("Closing My-IDM...", 10),
                "the first progress report must be the opening status",
            )
            self.assertEqual(
                splash.messages[-1],
                ("Goodbye!", 100),
                "the last progress report must be the 100% goodbye",
            )
            self.assertTrue(
                splash.closed,
                "the exit splash must be closed once shutdown finishes",
            )

            shutdown_steps = [msg for msg, _ in splash.messages]
            self.assertTrue(
                any("Shutdown complete." in msg for msg in shutdown_steps),
                f"manager.stop() must feed the exit splash its status callback, "
                f"got {shutdown_steps!r}",
            )
            percentages = [pct for _, pct in splash.messages]
            self.assertEqual(
                percentages,
                sorted(percentages),
                f"exit splash progress must advance monotonically, got {percentages!r}",
            )

            self.assertFalse(
                win.isVisible(),
                "with close-to-tray disabled the main window must really close",
            )
            self.assertTrue(
                manager._stopped,
                "a real close must shut the DownloadManager down",
            )
        finally:
            destroy_window(win, manager, db)

    def test_main_window_close_to_tray_ignores_close_event(self):
        """close-to-tray must ignore the close event, hide the window, and keep running."""
        from my_idm.database import Database
        from my_idm.manager import DownloadManager
        from my_idm.main_window import MainWindow

        db = Database(":memory:")
        db.open()
        manager = DownloadManager(db)
        manager.set_general_config(
            GeneralConfig(close_to_tray=True, enable_system_tray=True)
        )
        self.assertTrue(manager.general_config.close_to_tray)
        self.assertTrue(manager.general_config.enable_system_tray)

        win = MainWindow(manager, show_exit_splash=True)
        _RecordingExitSplash.instances.clear()
        try:
            with mock.patch(
                "my_idm.splash.IDMExitSplashScreen", _RecordingExitSplash
            ):
                win.show()
                QApplication.processEvents()
                self.assertTrue(win.isVisible())

                # close() is a no-op on this path: the event is ignored and the
                # window is merely hidden so downloads keep running in the tray.
                self.assertFalse(win.close(), "close() must report the event was ignored")
                QApplication.processEvents()

            self.assertFalse(
                win.isVisible(), "close-to-tray must hide the window, not destroy it"
            )
            self.assertEqual(
                _RecordingExitSplash.instances,
                [],
                "close-to-tray must not run the exit-splash shutdown path",
            )
            self.assertFalse(
                manager._stopped,
                "close-to-tray must leave the DownloadManager running",
            )
            self.assertTrue(
                win._details_timer.isActive(),
                "close-to-tray keeps the app alive, so the details timer must still run",
            )
            self.assertFalse(
                win._tor_availability_timer.isActive(),
                "closeEvent always stops the Tor availability probe",
            )
            # A second close is still swallowed: the window is not closable at all.
            self.assertFalse(win.close(), "close() must remain ignored while the tray is on")
        finally:
            destroy_window(win, manager, db)

    def test_main_window_close_without_exit_splash_flag_skips_splash(self):
        """show_exit_splash=False must still close cleanly, without any exit splash."""
        from my_idm.database import Database
        from my_idm.manager import DownloadManager
        from my_idm.main_window import MainWindow

        db = Database(":memory:")
        db.open()
        manager = DownloadManager(db)
        manager.set_general_config(
            GeneralConfig(close_to_tray=False, enable_system_tray=False)
        )

        win = MainWindow(manager, show_exit_splash=False)
        _RecordingExitSplash.instances.clear()
        try:
            with mock.patch(
                "my_idm.splash.IDMExitSplashScreen", _RecordingExitSplash
            ):
                win.show()
                QApplication.processEvents()
                win.close()
                QApplication.processEvents()

            self.assertEqual(
                _RecordingExitSplash.instances,
                [],
                "no exit splash may be constructed when the flag is off",
            )
            self.assertFalse(win.isVisible())
            self.assertTrue(manager._stopped, "the window must still shut the manager down")
        finally:
            destroy_window(win, manager, db)


if __name__ == "__main__":
    unittest.main()
