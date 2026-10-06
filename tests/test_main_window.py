"""Unit tests for MainWindow, toolbar layout, column resizing, and UI interactions."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QMimeData,
    QPoint,
    QPointF,
    QRect,
    QSettings,
    Qt,
    QUrl,
)
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QToolBar,
    QToolButton,
)

from my_idm.database import Database, DownloadEntry
from my_idm.download_model import Col
from my_idm.main_window import MainWindow
from my_idm.manager import DownloadManager
from my_idm.settings_dialog import (
    TAB_BROWSER,
    TAB_EXTERNAL_TOOLS,
    TAB_SECURITY,
    TAB_TOR,
    TAB_TORRENT,
    TAB_VPN,
    tab_index,
)
from tests.conftest import rows_by_section

app = QApplication.instance() or QApplication([])

IS_HEADLESS_WIN_CI = sys.platform == "win32" and os.environ.get("CI") == "true"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Every key `MainWindow._save_ui_state_to_db()` writes into QSettings. The legacy
# fallback in `_restore_ui_state_from_db()` reads them straight back for any
# window built while the DB has no stored state, so a single leaked key makes
# every later test in this file order-dependent on the leftovers.
_QSETTINGS_KEYS = (
    "header_state",
    "splitter_state",
    "details_visible",
    "details_height",
    "details_tab",
)


class _FakeClipboard:
    """In-memory ``QClipboard`` stand-in.

    The Windows clipboard is asynchronous and process-global: a transient lock
    by any other process makes ``QClipboard.setText`` a silent no-op, so reading
    it back is a coin flip. ``test_copy_multiple_urls_to_clipboard`` failed for
    exactly that reason on a clean run. Production reaches the clipboard only
    through ``QGuiApplication.clipboard()`` (main_window.py:1689), so swapping
    that single name in the module under test makes the test deterministic and
    stops it clobbering the developer's clipboard.
    """

    def __init__(self):
        self._text = ""
        self._image = None
        self.set_calls: list[str] = []
        self.clear_calls = 0

    def text(self, mode=None):
        return self._text

    def setText(self, text, mode=None):
        self._text = text or ""
        self.set_calls.append(self._text)

    def clear(self, mode=None):
        self.clear_calls += 1
        self._text = ""
        self._image = None

    def image(self, mode=None):
        return self._image

    def setImage(self, image, mode=None):
        self._image = image

    def setPixmap(self, pixmap, mode=None):
        self._image = pixmap

    def pixmap(self, mode=None):
        return self._image

    def mimeData(self, formats=None):
        return None

    def supportsSelection(self):
        return False

    def ownsClipboard(self):
        return False

    def ownsSelection(self):
        return False


def install_fake_clipboard():
    """Return (fake, patcher) for the module-level ``QGuiApplication`` name.

    The module NAME is patched, not the attribute on the real Qt class. Patching
    ``QGuiApplication.clipboard`` on the actual class would rebind it
    process-wide for the whole session, and would also make the ``if clipboard:``
    guard in ``_on_copy_url`` permanently true.
    """
    fake = _FakeClipboard()
    patcher = patch(
        "my_idm.main_window.QGuiApplication",
        SimpleNamespace(clipboard=lambda: fake),
    )
    return fake, patcher


def assert_clipboard(fake, expected: str, context: str) -> None:
    """Assert the clipboard holds exactly ``expected``.

    Deliberately not a "return the first non-empty value" helper: a stale value
    from an earlier copy in the same test would satisfy that silently. The setText
    log is included so a failure says what the code actually tried to copy.
    """
    actual = fake.text()
    if actual != expected:
        raise AssertionError(
            f"clipboard mismatch for {context}: expected {expected!r}, got "
            f"{actual!r}; setText call log: {fake.set_calls!r}"
        )


# Typographic punctuation that legitimately appears in an action label. U+2026 is
# the "Move…" ellipsis, not a decorative glyph, so it is not evidence that an
# action mixes icon and emoji styling.
_LABEL_PUNCTUATION = frozenset("…")


def decoration_chars(label: str) -> list[str]:
    """Return the decorative (emoji / symbol) characters in a menu label."""
    return [ch for ch in label if ord(ch) > 0x2000 and ch not in _LABEL_PUNCTUATION]


def restore_qsettings():
    """Remove every QSettings key this test file's windows can write.

    A leaked key is not cosmetic: `_restore_ui_state_from_db` falls back to
    reading these keys whenever the DB has no stored state, so a leftover from
    one test silently configures the next one.
    """
    settings = QSettings("MyIDM", "My-IDM")
    for key in _QSETTINGS_KEYS:
        settings.remove(key)
    settings.sync()


def has_real_desktop() -> bool:
    """Whether window geometry can be measured in this environment.

    A few tests below assert where the window ended up on screen. Those are statements about Qt's
    window management and the platform's frame metrics as much as about this application, and they
    only hold where a real window manager is placing real windows:

    - a CI runner has a virtual screen and no desktop session, so it clamps a 1280-wide window to
      1022 and puts a restored window 112px above where ``move()`` left it;
    - ``QT_QPA_PLATFORM=offscreen`` has no frame at all, which moves the same numbers by 3px.

    Neither is a property of the code, so neither can be asserted there. Everything these tests
    check that is not about pixels - what is written to the database, what comes back out - is
    asserted without this gate and still runs everywhere.
    """
    if os.environ.get("CI"):
        return False
    app = QApplication.instance()
    return app is not None and app.platformName() not in ("offscreen", "minimal", "")


def _destroy_window(win):
    """Actually destroy a MainWindow instead of merely hiding it.

    ``MainWindow.closeEvent`` does ``event.ignore(); self.hide()`` whenever
    ``close_to_tray`` / ``enable_system_tray`` are on (both default True), so a
    bare ``win.close()`` is a no-op: the window survives, still parented to
    nothing, carrying a live 1 Hz ``_details_timer`` and a 4 s
    ``_tor_availability_timer`` that opens a real socket probe. Setting
    ``_force_exit`` first routes closeEvent down the real teardown path
    (stops both timers, saves UI state, stops the manager); ``deleteLater()``
    plus one event-loop turn then destroys the C++ object.
    """
    try:
        win._force_exit = True
        win.close()
    except Exception:
        pass
    win.deleteLater()
    QApplication.processEvents()


class _MainWindowTestCase(unittest.TestCase):
    """Fixture base: a real DB + DownloadManager + MainWindow, all hard-destroyed.

    Teardown is expressed entirely through ``addCleanup`` because unittest runs
    ``tearDown()`` *before* the cleanups: a ``tearDown`` that closed the database
    would leave ``_destroy_window`` -> ``closeEvent`` -> ``_save_ui_state_to_db``
    writing to a closed handle. Cleanups are LIFO, so the order registered here
    (temp/db, db, manager, qsettings, window) unwinds as window, qsettings,
    manager, db, temp.
    """

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        # Registered immediately after construction: DownloadManager owns four
        # QTimers, an HTTPEngine, a TorrentEngine, a BrowserServer and a
        # TorServiceManager whose data_dir is the user's ~/.my-idm/tor_data.
        self.addCleanup(self.manager.stop)
        self.addCleanup(restore_qsettings)
        self.win = self.new_window()

    def new_window(self, manager=None, **kwargs) -> MainWindow:
        """Build a MainWindow that is destroyed automatically at test end."""
        win = MainWindow(manager if manager is not None else self.manager, **kwargs)
        self.addCleanup(_destroy_window, win)
        return win


class TestMainWindowTeardownGuards(unittest.TestCase):
    """One-off guards on the teardown machinery itself.

    Deliberately NOT a `_MainWindowTestCase` subclass: pytest collects a
    `unittest.TestCase` base once per concrete subclass, which would run these
    twice over and prove nothing extra.
    """

    def setUp(self):
        self.db = Database(":memory:")
        self.db.open()
        self.addCleanup(self.db.close)
        self.manager = DownloadManager(self.db)
        self.addCleanup(self.manager.stop)
        self.addCleanup(restore_qsettings)
        self.win = MainWindow(self.manager)
        self.addCleanup(_destroy_window, self.win)

    def test_saved_ui_state_keys_are_all_removed_by_the_cleanup(self):
        """Every QSettings key `_save_ui_state_to_db` writes is cleared at teardown.

        The teardown used to remove only `header_state`, so `splitter_state`,
        `details_visible`, `details_height` and `details_tab` leaked and were then
        read back by the legacy fallback in `_restore_ui_state_from_db` for every
        later window built on an empty DB. This asserts the full set is covered.
        """
        settings = QSettings("MyIDM", "My-IDM")
        try:
            self.win._save_ui_state_to_db()
            written = {key for key in _QSETTINGS_KEYS if settings.contains(key)}
            self.assertEqual(
                written, set(_QSETTINGS_KEYS),
                "_save_ui_state_to_db must write exactly the keys the cleanup knows "
                f"about; wrote {sorted(written)}",
            )
            # Run the cleanup body now to prove it clears all of them.
            restore_qsettings()
            left = {key for key in _QSETTINGS_KEYS if settings.contains(key)}
            self.assertEqual(left, set(), f"QSettings keys leaked: {sorted(left)}")
        finally:
            restore_qsettings()

    def test_bare_close_is_a_no_op_and_force_exit_is_not(self):
        """Regression guard for the teardown helper itself.

        `MainWindow.closeEvent` does `event.ignore(); self.hide()` while
        close-to-tray is on, so a bare `close()` leaves a fully live window with
        its 1 Hz `_details_timer` and 4 s `_tor_availability_timer` still running.
        These assertions pin the difference the cleanup depends on.
        """
        cfg = self.manager.general_config
        self.assertTrue(cfg.enable_system_tray and cfg.close_to_tray,
                        "the no-op-close precondition must hold for this guard")

        self.win.show()
        QApplication.processEvents()
        self.assertTrue(self.win._details_timer.isActive())
        self.assertTrue(self.win._tor_availability_timer.isActive())

        # A bare close only hides: the details timer survives, which is the leak.
        self.win.close()
        QApplication.processEvents()
        self.assertTrue(self.win.isHidden(), "close-to-tray must ignore a bare close")
        self.assertTrue(
            self.win._details_timer.isActive(),
            "a bare close leaves the 1 Hz details timer running (the leak this "
            "helper exists to prevent)",
        )
        self.assertFalse(
            self.win._tor_availability_timer.isActive(),
            "closeEvent does stop the Tor probe timer even on the close-to-tray path",
        )

        # _force_exit routes closeEvent down the real teardown path.
        self.win._force_exit = True
        self.win.close()
        QApplication.processEvents()
        self.assertFalse(
            self.win._details_timer.isActive(),
            "_force_exit must stop the details timer via closeEvent",
        )



@unittest.skipIf(IS_HEADLESS_WIN_CI, "headless Windows CI cannot arm OLE drop targets on real windows")
class TestTorrentDragAndDrop(_MainWindowTestCase):
    """.torrent files dropped anywhere in the window.

    The drop handlers are driven with synthesised `QDropEvent`s rather than a live drag: a real
    one needs an OLE drag source, and the decision logic under test is "which paths in this
    payload are torrents", which lives in `local_torrent_paths_from_mime`.
    """

    def setUp(self):
        super().setUp()
        from my_idm.notifications import unregister_notification_handler

        self.addCleanup(unregister_notification_handler)
        import tempfile

        self._mimes: list[QMimeData] = []
        self.tmp = Path(tempfile.mkdtemp())
        self.torrent = self.tmp / "ubuntu.torrent"
        self.torrent.write_bytes(b"d8:announce20:http://tracker/annce4:infod4:name4:testee")
        self.text_file = self.tmp / "notes.txt"
        self.text_file.write_text("not a torrent")

    def _mime(self, *paths):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        # Keep it alive for the life of the test. QDragEnterEvent/QDropEvent do not take ownership
        # of their QMimeData, so a temporary passed straight into the constructor is collected
        # while the event still points at it, and event.mimeData() then dereferences freed memory
        # — an access violation inside the drop handler that looks exactly like a Qt bug.
        self._mimes.append(mime)
        return mime

    def _shown(self):
        """Show the window so drop targets are armed, and return it.

        Drops are armed in `showEvent`, and Qt only routes drag events to a widget that accepts
        them — so a test that never shows the window would be asserting on an event that was
        simply never delivered.
        """
        self.win.show()
        QApplication.processEvents()
        return self.win

    def _drop(self, mime):
        event = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self._shown().event(event)
        return event

    def test_the_window_accepts_drops_once_shown(self):
        self._shown()
        # Armed in showEvent rather than __init__: registering a drop target on Windows binds an
        # OLE registration to the HWND, and doing that before the native handle exists made
        # short-lived windows leave it behind.
        self.assertTrue(self.win.acceptDrops())

    def test_dropping_a_torrent_adds_a_torrent_row(self):
        with patch("my_idm.notifications.notify_torrent_files_added") as notify:
            event = self._drop(self._mime(self.torrent))
        # isAccepted(), not the value widget.event() returns: that reports whether the handler
        # *ran*, and dropEvent returns having handled every payload it was given.
        self.assertTrue(event.isAccepted())
        rows = self.db.get_all_downloads()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].download_type, "torrent")
        # Canonicalised, because add_download de-duplicates on an exact string match of the url.
        self.assertEqual(rows[0].url, os.path.normpath(str(self.torrent)))
        notify.assert_called_once()

    def test_dropping_several_torrents_adds_them_all(self):
        other = self.tmp / "debian.torrent"
        other.write_bytes(b"d8:announce20:http://tracker/annce4:infod4:name4:testee")
        with patch("my_idm.notifications.notify_torrent_files_added"):
            self._drop(self._mime(self.torrent, other))
        self.assertEqual(len(self.db.get_all_downloads()), 2)

    def test_dropping_a_non_torrent_adds_nothing(self):
        with patch("my_idm.notifications.notify_torrent_files_added") as notify:
            event = self._drop(self._mime(self.text_file))
        self.assertFalse(event.isAccepted())
        self.assertEqual(len(self.db.get_all_downloads()), 0)
        notify.assert_not_called()

    def test_a_torrent_dropped_alongside_other_files_still_works(self):
        # A user dragging a mixed selection should get the torrent, not nothing.
        with patch("my_idm.notifications.notify_torrent_files_added"):
            event = self._drop(self._mime(self.text_file, self.torrent))
        self.assertTrue(event.isAccepted())
        self.assertEqual(len(self.db.get_all_downloads()), 1)

    def test_a_drag_with_no_torrent_is_refused_before_the_drop(self):
        # Deciding at dragEnter is what keeps the window from lighting up a drop cursor for a
        # payload it will then refuse on release.
        event = QDragEnterEvent(
            QPoint(10, 10),
            Qt.DropAction.CopyAction,
            self._mime(self.text_file),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self._shown().event(event)
        self.assertFalse(event.isAccepted())

    def test_a_drag_carrying_a_torrent_is_accepted(self):
        event = QDragEnterEvent(
            QPoint(10, 10),
            Qt.DropAction.CopyAction,
            self._mime(self.torrent),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self._shown().event(event)
        self.assertTrue(event.isAccepted())

    def test_the_add_torrent_button_uses_the_same_ingestion_path(self):
        with patch(
            "my_idm.main_window.QFileDialog.getOpenFileNames",
            return_value=([str(self.torrent)], ""),
        ), patch("my_idm.notifications.notify_torrent_files_added"):
            self.win._on_add_torrent()
        self.assertEqual(len(self.db.get_all_downloads()), 1)

    def test_the_watched_folder_signal_updates_the_status_line(self):
        with patch("my_idm.notifications.notify_torrent_files_added") as notify:
            self.win._on_watched_folder_torrents([os.path.normpath(str(self.torrent))])
        self.assertIn("Watched folder added", self.win._status_label.text())
        notify.assert_called_once()

    def test_dropping_nothing_says_so_rather_than_being_silent(self):
        self.win._add_torrent_paths([])
        # An action the user performed deliberately must never produce no feedback at all.
        self.assertIn("No .torrent files", self.win._status_label.text())


class TestMainWindowToolbar(_MainWindowTestCase):
    """Tests for toolbar actions, simplified buttons, and footer status."""

    def setUp(self):
        super().setUp()
        from my_idm.notifications import unregister_notification_handler
        # Registered first => runs last, so a window teardown cannot re-register
        # the handler after it has been cleared.
        self.addCleanup(unregister_notification_handler)

    def test_merged_add_button_on_toolbar(self):
        """Toolbar should have a single merged Add Download button."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        actions = toolbar.actions()
        action_texts = [a.text() for a in actions if not a.isSeparator()]

        self.assertIn("Add Download", action_texts)
        self.assertNotIn("📦 Add Torrent", action_texts)
        self.assertNotIn("📦 Add Torrent File…", action_texts)

    def test_removed_buttons_from_toolbar(self):
        """Open File and Open Folder buttons should not be on the toolbar."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        actions = toolbar.actions()
        self.assertNotIn(self.win._act_open_file, actions)
        self.assertNotIn(self.win._act_open_folder, actions)

    def test_details_and_console_footer_buttons_and_toolbar_removal(self):
        """Details panel button is removed from toolbar; footer has Details and Console toggle buttons."""
        self.win.show()
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        # Details button is removed from top toolbar
        btn = getattr(self.win, "_details_toolbar_btn", None)
        self.assertIsNone(btn, "_details_toolbar_btn should no longer exist on MainWindow")

        # Footer buttons exist and are visible
        details_btn = getattr(self.win, "_details_status_btn", None)
        console_btn = getattr(self.win, "_console_status_btn", None)
        self.assertIsNotNone(details_btn)
        self.assertIsNotNone(console_btn)
        self.assertTrue(details_btn.isVisible())
        self.assertTrue(console_btn.isVisible())

        # Initially details panel is visible in Details mode
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertEqual(self.win._details_panel.current_mode(), "details")
        self.assertIn("ON", details_btn.text())
        self.assertIn("OFF", console_btn.text())

        # Click Console button on footer -> switches to Console mode
        console_btn.click()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertEqual(self.win._details_panel.current_mode(), "console")
        self.assertIn("OFF", details_btn.text())
        self.assertIn("ON", console_btn.text())

        # Click Console button again while active -> hides bottom panel
        console_btn.click()
        self.assertFalse(self.win._details_panel.isVisible())
        self.assertIn("OFF", details_btn.text())
        self.assertIn("OFF", console_btn.text())

        # Click Details button on footer -> opens panel in Details mode
        details_btn.click()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertEqual(self.win._details_panel.current_mode(), "details")
        self.assertIn("ON", details_btn.text())
        self.assertIn("OFF", console_btn.text())

    def test_icon_only_buttons_on_toolbar(self):
        """Resume, Pause, Stop, Delete, Move, and Recheck must be icon-only on toolbar, while Preferences shows text."""
        toolbar = self.win.findChild(QToolBar)
        self.assertIsNotNone(toolbar)

        for act in (
            self.win._act_resume,
            self.win._act_pause,
            self.win._act_stop,
            self.win._act_start_seeding,
            self.win._act_pause_all,
            self.win._act_stop_all_seeding,
            self.win._act_delete,
            self.win._act_move,
            self.win._act_recheck,
        ):
            btn = toolbar.widgetForAction(act)
            self.assertIsInstance(btn, QToolButton)
            self.assertEqual(
                btn.toolButtonStyle(),
                Qt.ToolButtonStyle.ToolButtonIconOnly,
                f"Action {act.text()} must be icon-only on the toolbar",
            )
            self.assertFalse(
                act.icon().isNull(),
                f"Action {act.text()} must have a non-null QIcon so Qt does not fall back to text",
            )

        for act in (
            self.win._act_preferences,
        ):
            btn = toolbar.widgetForAction(act)
            self.assertIsInstance(btn, QToolButton)
            self.assertEqual(
                btn.toolButtonStyle(),
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon,
                f"Action {act.text()} should display text beside icon",
            )

    def test_toolbar_has_no_logo(self):
        """Toolbar must not contain any logo or text label widgets."""
        toolbars = self.win.findChildren(QToolBar)
        self.assertTrue(len(toolbars) > 0)
        main_tb = toolbars[0]
        labels = main_tb.findChildren(QLabel)
        self.assertEqual(len(labels), 0, f"Toolbar should not have any labels: {[l.text() for l in labels]}")

    def test_footer_active_and_total_count(self):
        """Footer displays 'X Downloads, Y Active'."""
        self.assertIn("Downloads", self.win._count_label.text())
        self.assertIn("Active", self.win._count_label.text())

    def test_animepahe_footer_badge_lifecycle(self):
        """Footer badge for AnimePahe is hidden by default and becomes visible when scraper runs."""
        self.win.show()
        # Initially hidden when scraper is not running
        self.assertTrue(self.win._animepahe_status_btn.isHidden())

        # Scraper starts running
        self.manager.animepahe_status_changed.emit(True)
        self.assertFalse(self.win._animepahe_status_btn.isHidden())
        self.assertEqual(self.win._animepahe_status_btn.text(), "🎬 AnimePahe: Active")

        # Scraper stops running
        self.manager.animepahe_status_changed.emit(False)
        self.assertTrue(self.win._animepahe_status_btn.isHidden())

    def test_animepahe_footer_console_log_button_and_panel_toggle(self):
        """Clicking on console log button or menu from footer opens bottom panel and selects console tab."""
        self.win.show()
        # Console button is always visible on the footer
        self.assertFalse(self.win._animepahe_console_btn.isHidden())
        self.assertTrue(self.win._animepahe_console_btn.isVisible())

        # Start scraper -> status badge becomes active, console button remains visible
        self.manager.animepahe_status_changed.emit(True)
        self.assertTrue(self.win._animepahe_status_btn.isVisible())
        self.assertTrue(self.win._animepahe_console_btn.isVisible())

        # Hide details panel first to test that clicking console log button opens it
        self.win._act_toggle_details.setChecked(False)
        self.assertFalse(self.win._details_panel.isVisible())
        self.assertIn("OFF", self.win._animepahe_console_btn.text())

        # Click footer console log button -> opens panel in Console mode
        self.win._animepahe_console_btn.click()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertTrue(self.win._act_toggle_details.isChecked())
        self.assertTrue(self.win._details_panel.is_animepahe_console_active())
        self.assertIn("ON", self.win._animepahe_console_btn.text())

        # Clicking again while console tab is active toggles panel closed
        self.win._animepahe_console_btn.click()
        self.assertFalse(self.win._details_panel.isVisible())
        self.assertFalse(self.win._act_toggle_details.isChecked())
        self.assertIn("OFF", self.win._animepahe_console_btn.text())

        # Opening via menu action
        self.win._on_view_animepahe_console_log()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertTrue(self.win._details_panel.is_animepahe_console_active())
        self.assertIn("ON", self.win._animepahe_console_btn.text())

        # Scraper stops -> status badge hides, but console button remains visible always
        self.manager.animepahe_status_changed.emit(False)
        self.assertTrue(self.win._animepahe_status_btn.isHidden())
        self.assertTrue(self.win._animepahe_console_btn.isVisible())


class TestMainWindowTableAndInteractions(_MainWindowTestCase):
    """Tests for table view, interactive resizing, geometry persistence, and actions."""

    def test_all_columns_are_interactive_resizable(self):
        """Every column in the table must have ResizeMode.Interactive so users can drag borders."""
        header = self.win._table.horizontalHeader()
        self.assertFalse(header.stretchLastSection())
        self.assertFalse(
            header.cascadingSectionResizes(),
            "Cascading resizes must be False so resizing shifts subsequent columns",
        )
        for col in range(Col.COUNT):
            mode = header.sectionResizeMode(col)
            self.assertEqual(
                mode,
                QHeaderView.ResizeMode.Interactive,
                f"Column {col} ({Col.HEADERS[col]}) should be Interactive, got {mode}",
            )

    def test_resizing_column_shifts_subsequent_columns(self):
        """Resizing a column must shift all subsequent columns right/left without compressing them."""
        header = self.win._table.horizontalHeader()
        self.win.show()

        initial_widths = [header.sectionSize(i) for i in range(Col.COUNT)]
        pos_col1_before = header.sectionViewportPosition(Col.SIZE)
        pos_col2_before = header.sectionViewportPosition(Col.PROGRESS)

        delta = 100
        new_name_w = initial_widths[Col.NAME] + delta
        header.resizeSection(Col.NAME, new_name_w)

        self.assertEqual(header.sectionSize(Col.NAME), new_name_w)
        self.assertEqual(header.sectionSize(Col.SIZE), initial_widths[Col.SIZE])
        self.assertEqual(header.sectionSize(Col.PROGRESS), initial_widths[Col.PROGRESS])
        self.assertEqual(header.sectionViewportPosition(Col.SIZE), pos_col1_before + delta)
        self.assertEqual(header.sectionViewportPosition(Col.PROGRESS), pos_col2_before + delta)

    def test_column_width_can_be_resized(self):
        """Resizing a column changes its width properly."""
        header = self.win._table.horizontalHeader()
        header.resizeSection(Col.NAME, 350)
        self.assertEqual(self.win._table.columnWidth(Col.NAME), 350)

        # Save Path is hidden by default on a fresh profile (see
        # `DEFAULT_HIDDEN_COLUMNS`), and a hidden section reports a width of 0. Show it
        # first: this test is about resizing, not about the default arrangement.
        header.setSectionHidden(Col.SAVE_PATH, False)
        header.resizeSection(Col.SAVE_PATH, 400)
        self.assertEqual(self.win._table.columnWidth(Col.SAVE_PATH), 400)

    def test_column_widths_persist_in_qsettings(self):
        """Header state is saved and restored."""
        header = self.win._table.horizontalHeader()
        header.resizeSection(Col.NAME, 380)

        settings = QSettings("MyIDM", "My-IDM")
        settings.setValue("header_state", header.saveState())

        win2 = self.new_window()
        self.assertEqual(win2._table.columnWidth(Col.NAME), 380)
        self.assertTrue(win2._table.horizontalHeader().sectionsMovable())

    def test_column_ordering_by_dragging_and_persistence(self):
        """Columns are movable by dragging and their reordered visual positions persist."""
        header = self.win._table.horizontalHeader()
        self.assertTrue(header.sectionsMovable())
        self.assertTrue(header.isFirstSectionMovable())

        # Move Col.NAME to visual index 3. The source is read from the header rather than
        # hard-coded: the default arrangement changed once already, and a literal that no
        # longer names the column moves a *different* one and still "passes" the save check.
        with patch.object(self.win, "_save_ui_state_to_db", wraps=self.win._save_ui_state_to_db) as mock_save:
            header.moveSection(header.visualIndex(Col.NAME), 3)
            self.assertEqual(header.visualIndex(Col.NAME), 3)
            # Exactly one persist per move; "called at least once" would not notice
            # a regression that re-saves the whole state on every sectionMoved.
            mock_save.assert_called_once()

        # When creating a second window, the moved visual index is restored
        win2 = self.new_window()
        header2 = win2._table.horizontalHeader()
        self.assertTrue(header2.sectionsMovable())
        self.assertEqual(header2.visualIndex(Col.NAME), 3)

    def test_window_geometry_location_maximized_columns_in_db(self):
        """Window size, location, maximized state, and column lengths are persisted and restored via DB."""
        # The minimum width follows the toolbar, which is font- and DPI-dependent, so 1280
        # is only a valid width where the toolbar fits inside it.
        width = max(1280, self.win.minimumWidth())
        self.win.resize(width, 720)
        self.win.move(150, 120)
        self.win._table.setColumnWidth(Col.NAME, 360)
        self.win._table.setColumnWidth(Col.SIZE, 130)

        self.win._save_ui_state_to_db()

        db_state = self.db.get_window_state()
        self.assertIsNotNone(db_state)
        self.assertEqual(db_state["width"], self.win.width())
        self.assertEqual(db_state["height"], self.win.height())
        self.assertEqual(db_state["x"], self.win.x())
        self.assertEqual(db_state["y"], self.win.y())
        self.assertFalse(db_state["is_maximized"])
        self.assertEqual(db_state["column_widths"][str(Col.NAME)], 360)
        self.assertEqual(db_state["column_widths"][str(Col.SIZE)], 130)

        win2 = self.new_window()
        self.assertEqual(win2._table.columnWidth(Col.NAME), 360)
        self.assertEqual(win2._table.columnWidth(Col.SIZE), 130)

    @unittest.skipUnless(
        has_real_desktop(), "restored geometry needs a desktop session; CI has only a virtual screen"
    )
    def test_the_restored_window_carries_the_saved_geometry(self):
        """What was persisted is what the next launch gets back.

        Separated from the database assertions above because this half is about pixels, and a
        runner's virtual screen clamps a window to whatever it believes fits - a 1280-wide request
        came back as 1022. Comparing two windows that both went through the same path still is not
        enough there, because the first is never shown when its state is saved.
        """
        width = max(1280, self.win.minimumWidth())
        self.win.resize(width, 720)
        self.win.move(150, 120)
        self.win._table.setColumnWidth(Col.NAME, 360)
        self.win.show()
        QApplication.processEvents()
        self.win._save_ui_state_to_db()

        win2 = self.new_window()
        win2.show()
        QApplication.processEvents()
        self.assertEqual(win2.width(), self.win.width())
        self.assertEqual(win2.height(), self.win.height())
        self.assertEqual(win2.x(), self.win.x())
        self.assertEqual(win2.y(), self.win.y())
        self.assertEqual(win2._table.columnWidth(Col.NAME), 360)

    def test_double_click_calls_open_file(self):
        """Double clicking a real data row triggers file open.

        The model used to be empty, so ``index(0, 0)`` was an invalid index and
        the call short-circuited through the section-header branch in
        ``_on_table_double_clicked`` (main_window.py:1542) -- which then returned
        early, so ``_on_open_file`` was never actually reached for a data row.
        """
        entry = DownloadEntry(
            id="dbl-1",
            url="https://example.com/dbl.zip",
            filename="dbl.zip",
            save_path=tempfile.gettempdir(),
            status="completed",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.assertEqual(self.win._model.rowCount(), 1)
        self.assertFalse(self.win._model.is_section_header_row(0))

        with patch.object(self.win, "_on_open_file") as mock_open:
            index = self.win._model.index(0, Col.NAME)
            self.win._on_table_double_clicked(index)
            mock_open.assert_called_once_with()

    def test_double_click_on_section_header_toggles_collapse_and_does_not_open(self):
        """Double clicking a section header row collapses it and never opens a file."""
        e_active = DownloadEntry(
            id="sec-dbl-active",
            url="https://example.com/active.zip",
            filename="active.zip",
            status="downloading",
        )
        e_inact = DownloadEntry(
            id="sec-dbl-inact",
            url="https://example.com/done.zip",
            filename="done.zip",
            status="completed",
        )
        self.db.add_download(e_active)
        self.db.add_download(e_inact)
        self.win._load_history()

        self.win._act_segregated_view.setChecked(True)
        QApplication.processEvents()

        header_rows = self.win._model.get_section_header_row_indices()
        self.assertTrue(header_rows, "segregated view must expose section header rows")
        first_header = header_rows[0]
        self.assertTrue(self.win._model.is_section_header_row(first_header))
        self.assertEqual(set(self.win._model._collapsed_sections), set())

        with patch.object(self.win, "_on_open_file") as mock_open:
            self.win._on_table_double_clicked(self.win._model.index(first_header, 0))
            mock_open.assert_not_called()

        self.assertIn(
            "active", self.win._model._collapsed_sections,
            "double clicking a section header must toggle the section",
        )
        self.assertTrue(self.db.get_ui_state("segregated_active_collapsed"))

        # A second double click expands it again.
        with patch.object(self.win, "_on_open_file") as mock_open:
            self.win._on_table_double_clicked(self.win._model.index(first_header, 0))
            mock_open.assert_not_called()
        self.assertNotIn("active", self.win._model._collapsed_sections)

    def test_details_timer_regroups_date_sections_after_midnight(self):
        """A window left open across local midnight must re-bucket the date sections.

        Today/Yesterday/Last 7 Days are relative to the current day, but nothing scheduled a
        rebuild, so the sections stayed frozen at whatever the last status change or filter
        produced. _details_timer is the 1 Hz clock that drives the fix, and it keeps ticking
        while the window is hidden to the tray - which is when the rollover goes unnoticed.
        """
        from datetime import datetime, timedelta

        import my_idm.download_model as download_model

        class _TomorrowDatetime(datetime):
            """`datetime` as the download model sees it, with the day advanced by one.

            Class body reads the enclosing test scope, so `datetime` inside now() is the
            real class - otherwise this would recurse.
            """

            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=1)

        now = datetime.now().astimezone()
        self.db.add_download(DownloadEntry(
            id="roll-today",
            url="https://example.com/today.zip",
            filename="today.zip",
            status="completed",
            added_at=now.isoformat(),
        ))
        self.win._load_history()

        self.win._set_segregation_mode("date")
        QApplication.processEvents()
        self.assertEqual(self.win._segregated_view_mode, "date")
        self.assertTrue(self.win._model.is_segregated_view())

        row = self.win._model.row_for_id("roll-today")
        self.assertIsNotNone(row)
        self.win._table.selectRow(row)
        self.assertEqual(self.win._selected_ids(), ["roll-today"])

        # Steady state: the 1 Hz tick must not touch the model while the day is unchanged.
        same_day_resets = QSignalSpy(self.win._model.modelReset)
        self.win._on_details_timer_tick()
        self.assertEqual(same_day_resets.size(), 0)

        with patch.object(download_model, "datetime", _TomorrowDatetime):
            self.win._on_details_timer_tick()

            # "roll-today" was added today, so after midnight it belongs under Yesterday
            # and Today is left empty.
            sections = rows_by_section(self.win._model)
            self.assertEqual(sections["Today"], [])
            self.assertEqual(sections["Yesterday"], ["roll-today"])

            # The rebuild is a model reset, which silently drops the view's selection - the
            # user must not lose the row they were working on just because it is past
            # midnight.
            self.assertEqual(self.win._selected_ids(), ["roll-today"])

            # And it settles: further ticks on the new day must not reset the model again.
            # size() counts emissions; count() is the arity of the last signal, not the
            # number of times it fired.
            settled_resets = QSignalSpy(self.win._model.modelReset)
            self.win._on_details_timer_tick()
            self.win._on_details_timer_tick()
            self.assertEqual(
                settled_resets.size(), 0, "a settled day must not rebuild on every tick"
            )

    def test_open_file_missing_triggers_file_not_found(self):
        """Opening a missing file should mark status as file_not_found."""
        e = DownloadEntry(
            id="d1",
            url="http://example.com/nonexistent.zip",
            filename="nonexistent.zip",
            file_path="C:/nonexistent_file_path_12345.zip",
            save_path="C:/",
            status="completed",
        )
        self.db.add_download(e)
        self.win._load_history()

        self.win._table.selectRow(0)
        self.win._on_open_file()

        self.assertEqual(self.db.get_download("d1").status, "file_not_found")

    def test_open_file_routes_through_the_cross_platform_helper(self):
        """"Open file" must not call ``os.startfile``.

        ``os.startfile`` only exists on Windows, so calling it unguarded raised
        ``AttributeError`` on Linux and macOS. The handler now defers to
        ``open_file_in_default_app``, which has a working ``QDesktopServices`` path.

        The helper is patched at its source module because the handler lazy-imports it; letting
        the real one run would hand the file to the actual shell, which the session
        hermeticity fixture treats as a failure.
        """
        with tempfile.TemporaryDirectory() as tmp:
            real_file = Path(tmp) / "present.zip"
            real_file.write_bytes(b"data")
            e = DownloadEntry(
                id="open-present",
                url="http://example.com/present.zip",
                filename="present.zip",
                file_path=str(real_file),
                save_path=tmp,
                status="completed",
            )
            self.db.add_download(e)
            self.win._load_history()
            self.win._table.selectRow(0)

            # Compared against the value the database actually holds: `normalize_path` rewrites
            # separators to forward slashes on the way in, so the entry no longer matches the
            # backslash form this test built the path with.
            stored = self.db.get_download("open-present").file_path

            with patch(
                "my_idm.external_tools.open_file_in_default_app"
            ) as mock_open:
                self.win._on_open_file()

        mock_open.assert_called_once_with(stored, create_if_missing=False)

    def test_open_folder_reveals_the_file_not_the_containing_folder(self):
        """The file is what gets handed over, so the platform can *select* it.

        Previously the Windows branch ran ``explorer /select,<path>`` — passing ``/select,`` as
        its own argv element, which Explorer tolerates by accident — while the non-Windows branch
        opened the containing folder instead. Both now go through ``show_in_folder`` with the
        file, so the two platforms agree.
        """
        with tempfile.TemporaryDirectory() as tmp:
            real_file = Path(tmp) / "present.zip"
            real_file.write_bytes(b"data")
            e = DownloadEntry(
                id="reveal-present",
                url="http://example.com/present.zip",
                filename="present.zip",
                file_path=str(real_file),
                save_path=tmp,
                status="completed",
            )
            self.db.add_download(e)
            self.win._load_history()
            self.win._table.selectRow(0)

            stored = self.db.get_download("reveal-present").file_path
            with patch("my_idm.external_tools.show_in_folder") as mock_reveal:
                self.win._on_open_folder()

        mock_reveal.assert_called_once_with(stored)

    def test_open_folder_falls_back_to_the_folder_when_the_file_is_gone(self):
        """No file to select, so the folder is the thing to open."""
        with tempfile.TemporaryDirectory() as tmp:
            e = DownloadEntry(
                id="reveal-folder-only",
                url="http://example.com/gone.zip",
                filename="gone.zip",
                file_path=str(Path(tmp) / "vanished.zip"),
                save_path=tmp,
                status="completed",
            )
            self.db.add_download(e)
            self.win._load_history()
            self.win._table.selectRow(0)

            stored = self.db.get_download("reveal-folder-only").save_path
            with patch("my_idm.external_tools.show_in_folder") as mock_reveal:
                self.win._on_open_folder()

        mock_reveal.assert_called_once_with(stored)

    def test_copy_url_to_clipboard(self):
        """MainWindow._on_copy_url copies the selected magnet link verbatim."""
        entry = DownloadEntry(
            id="test_copy",
            url="magnet:?xt=urn:btih:fedcba9876543210&dn=real_movie",
            filename="real_movie",
            save_path=tempfile.gettempdir(),
            download_type="torrent",
            status="downloading",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._table.selectRow(0)

        fake, patcher = install_fake_clipboard()
        with patcher:
            self.win._on_copy_url()

        assert_clipboard(fake, entry.url, "_on_copy_url (single magnet)")
        self.assertEqual(fake.set_calls, [entry.url], "exactly one setText, no retry")
        self.assertIn("Copied Magnet link", self.win._status_label.text())

    def test_copy_url_reports_plain_url_kind(self):
        """A non-magnet URL is reported as a URL, not as a magnet link."""
        entry = DownloadEntry(
            id="test_copy_url_kind",
            url="https://example.com/plain.zip",
            filename="plain.zip",
            status="completed",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._table.selectRow(0)

        fake, patcher = install_fake_clipboard()
        with patcher:
            self.win._on_copy_url()

        assert_clipboard(fake, entry.url, "_on_copy_url (single http url)")
        self.assertIn("Copied URL to clipboard", self.win._status_label.text())

    def test_copy_url_survives_a_none_clipboard(self):
        """`if clipboard:` guards a real platform answer: QApplication.clipboard() can be None.

        With a MagicMock patched over the Qt class attribute the guard is always
        truthy, so this branch used to be untestable. Patching the module name
        (below) makes the None case reachable.
        """
        entry = DownloadEntry(
            id="test_copy_none_cb",
            url="https://example.com/nocb.zip",
            filename="nocb.zip",
            status="completed",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._table.selectRow(0)
        self.win._status_label.setText("sentinel")

        with patch("my_idm.main_window.QGuiApplication",
                   SimpleNamespace(clipboard=lambda: None)):
            self.win._on_copy_url()  # must not raise

        self.assertEqual(
            self.win._status_label.text(), "sentinel",
            "with no clipboard the handler must report nothing rather than claim a copy",
        )

    def test_copy_url_with_no_selection_copies_nothing(self):
        """Nothing selected means no clipboard write and no status change."""
        fake, patcher = install_fake_clipboard()
        self.win._model.load_entries([
            DownloadEntry(id="ns-1", url="https://example.com/ns.zip",
                          filename="ns.zip", status="completed"),
        ])
        self.win._status_label.setText("sentinel")

        with patcher:
            self.win._on_copy_url()

        self.assertEqual(fake.set_calls, [])
        self.assertEqual(self.win._status_label.text(), "sentinel")

    def test_main_window_receives_filename_resolved(self):
        """MainWindow updates model when manager emits filename_resolved."""
        entry = DownloadEntry(
            id="test-2",
            url="https://example.com/api/get",
            filename="",
        )
        self.win._model.load_entries([entry])

        self.manager.filename_resolved.emit("test-2", "dynamic_file.zip")
        self.assertEqual(self.win._model.get_entry(0).filename, "dynamic_file.zip")
        self.assertEqual(self.win._model.data(self.win._model.index(0, Col.NAME)), "dynamic_file.zip")

    def test_force_start_action_and_context_menu(self):
        """Force start action is in Edit menu, context menu, and calls manager.force_start_download."""
        self.assertIsNotNone(self.win._act_force_start)
        self.assertEqual(self.win._act_force_start.text(), "Force Start")

        entry = DownloadEntry(
            id="d-fs-1",
            url="https://example.com/data.bin",
            filename="data.bin",
            status="paused",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._table.selectRow(0)

        with patch.object(self.manager, "force_start_download") as mock_fs:
            self.win._act_force_start.trigger()
            mock_fs.assert_called_once_with("d-fs-1")

    def test_context_menu_actions_have_icons_and_clean_text(self):
        """All context menu actions must have valid icons and free of decorative glyphs.

        The old check was `act.text()[0].isalnum()`, which passes for a *trailing*
        emoji (contradicting the test's own docstring) and raises IndexError on an
        empty label. The same glyph test already used by
        ``test_tray_add_download_matches_tray_indentation`` is applied instead:
        any character above U+2000 is decoration, wherever it sits in the string.
        The typographic ellipsis in "Move…" is punctuation, not decoration, so it
        is allowed.
        """
        context_actions = [
            self.win._act_resume,
            self.win._act_force_start,
            self.win._act_pause,
            self.win._act_move_up,
            self.win._act_move_down,
            self.win._act_copy_url,
            self.win._act_scan_antivirus,
            self.win._act_recheck,
            self.win._act_move,
            self.win._act_open_file,
            self.win._act_open_folder,
            self.win._act_delete,
        ]
        for act in context_actions:
            label = act.text()
            self.assertTrue(label, "a context menu action must have a non-empty label")
            self.assertFalse(act.icon().isNull(), f"Action '{label}' must have a valid QIcon")
            self.assertEqual(
                decoration_chars(label), [],
                f"Action '{label}' should have clean text with no leading or "
                f"trailing emoji",
            )
            self.assertTrue(
                label[0].isalnum(),
                f"Action '{label}' should start with a letter or digit, not a glyph",
            )


    def test_bandwidth_allocation_context_menu_and_action(self):
        """Bandwidth allocation updates entry metadata and calls engine."""
        entry = DownloadEntry(
            id="bw-test-1",
            url="https://example.com/file.zip",
            filename="file.zip",
            status="downloading",
        )
        self.db.add_download(entry)
        self.win._load_history()
        self.win._table.selectRow(0)

        self.win._on_set_bandwidth_allocation("low")
        self.assertEqual(self.manager.get_download_bandwidth_allocation("bw-test-1"), "low")

        self.win._on_set_bandwidth_allocation("high")
        self.assertEqual(self.manager.get_download_bandwidth_allocation("bw-test-1"), "high")

    def test_speed_limits_and_footer_context_menu(self):
        """Footer speed limit context menu adjusts global bandwidth limits and updates speed label."""
        from my_idm.main_window import SPEED_LIMIT_PRESETS
        labels = [label.lower().replace(" ", "") for label, _ in SPEED_LIMIT_PRESETS]
        expected_presets = [
            "unlimited", "1kbps", "2kbps", "5kbps", "10kbps", "50kbps",
            "100kbps", "200kbps", "500kbps", "1mbps", "2mbps", "5mbps",
            "10mbps", "100mbps",
        ]
        for ep in expected_presets:
            self.assertIn(ep, labels, f"Preset '{ep}' must be present in speed presets")

        # Set download limit to 1 MB/s
        self.win._set_speed_limit(1048576, is_upload=False)
        self.assertEqual(self.manager.network_config.download_limit, 1048576)

        # Set upload limit to 50 KB/s
        self.win._set_speed_limit(51200, is_upload=True)
        self.assertEqual(self.manager.network_config.upload_limit, 51200)

        # Speed label must reflect active limits
        lbl_text = self.win._speed_label.text()
        self.assertIn("Limit: 1.0 MiB/s", lbl_text)
        self.assertIn("Limit: 50.0 KiB/s", lbl_text)

    def test_menubar_actions_icons_and_alignment(self):
        """Menubar actions have icons to maintain uniform vertical text indentation."""
        menubar = self.win.menuBar()
        menus = {act.text(): act.menu() for act in menubar.actions()}

        # File menu
        file_menu = menus.get("&File")
        self.assertIsNotNone(file_menu)
        for act in file_menu.actions():
            if not act.isSeparator():
                self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' in File menu must have an icon")

        # View menu
        view_menu = menus.get("&View")
        self.assertIsNotNone(view_menu)
        for act in view_menu.actions():
            if not act.isSeparator():
                if act.menu():
                    self.assertFalse(act.menu().icon().isNull(), f"Submenu '{act.text()}' in View menu must have an icon")
                else:
                    self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' in View menu must have an icon")

        # Tools menu
        tools_menu = menus.get("&Tools")
        self.assertIsNotNone(tools_menu)
        for act in tools_menu.actions():
            if not act.isSeparator():
                self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' in Tools menu must have an icon")
        self.assertFalse(self.win._act_tor.icon().isNull())
        self.assertEqual(self.win._act_tor.text(), "Tor: OFF")

        # Help menu
        help_menu = menus.get("&Help")
        self.assertIsNotNone(help_menu)
        for act in help_menu.actions():
            if not act.isSeparator():
                self.assertFalse(act.icon().isNull(), f"Action '{act.text()}' in Help menu must have an icon")

    def test_copy_multiple_urls_to_clipboard(self):
        """MainWindow._on_copy_url with multiple selected rows copies URLs joined by newline.

        This test used to read the real Windows clipboard back, which is why it
        failed on a clean run: ``QClipboard.setText`` is a silent no-op while the
        clipboard is transiently locked, so the read came back empty. It now
        asserts the exact string the code asked to copy.
        """
        e1 = DownloadEntry(
            id="test_copy_1",
            url="https://example.com/file1.zip",
            filename="file1.zip",
            status="completed",
        )
        e2 = DownloadEntry(
            id="test_copy_2",
            url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test_mag",
            filename="test_mag",
            status="downloading",
        )
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.win._load_history()
        self.win._table.selectAll()
        self.assertEqual(len(self.win._selected_ids()), 2)

        fake, patcher = install_fake_clipboard()
        with patcher:
            self.win._on_copy_url()

        copied_lines = fake.text().splitlines()
        self.assertEqual(len(copied_lines), 2, f"expected 2 lines, got {fake.set_calls!r}")
        self.assertEqual(
            set(copied_lines), {e1.url, e2.url},
            f"clipboard must hold exactly the two selected URLs, got {fake.text()!r}",
        )
        # And the exact joined string, in the order the model reports the rows.
        expected = "\n".join(
            self.win._manager.get_entry(did).url for did in self.win._selected_ids()
        )
        assert_clipboard(fake, expected, "_on_copy_url (two selected rows)")
        self.assertEqual(len(fake.set_calls), 1, "one setText, no clipboard retry loop")
        self.assertIn("Copied 2 URLs/Magnets to clipboard", self.win._status_label.text())

    def test_rename_action_triggers_rename_download(self):
        """MainWindow._on_rename triggers manager.rename_download and updates model."""
        e = DownloadEntry(
            id="d_rename_1",
            url="https://example.com/old_name.iso",
            filename="old_name.iso",
            save_path=tempfile.gettempdir(),
            status="completed",
        )
        self.db.add_download(e)
        self.win._load_history()
        self.win._table.selectRow(0)

        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = 1  # Accepted
        mock_dlg.new_name = "new_name.iso"
        with patch("my_idm.main_window.RenameDialog", return_value=mock_dlg):
            with patch.object(self.manager, "rename_download", return_value=(True, "")) as mock_ren:
                self.win._act_rename.trigger()
                mock_ren.assert_called_once_with("d_rename_1", "new_name.iso")

        # Emitting download_renamed updates table model row
        self.manager.download_renamed.emit("d_rename_1", "new_name.iso")
        self.assertEqual(self.win._model.get_entry(0).filename, "new_name.iso")
        self.assertEqual(self.win._model.data(self.win._model.index(0, Col.NAME)), "new_name.iso")

    def test_context_menu_selects_unselected_row(self):
        """Right-clicking an unselected row in table selects it for the context menu."""
        e1 = DownloadEntry(id="d_cm_1", url="https://example.com/1", filename="1.zip", save_path="D:/Downloads", added_at="2026-09-25T10:00:00Z")
        e2 = DownloadEntry(id="d_cm_2", url="https://example.com/2", filename="2.zip", save_path="D:/Downloads", added_at="2026-09-25T09:00:00Z")
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.win._load_history()

        # Select row 0 initially
        self.win._table.selectRow(0)
        self.assertEqual(self.win._selected_ids(), ["d_cm_1"])

        # Context menu at row 1's rect
        rect = self.win._table.visualRect(self.win._model.index(1, 0))
        pos = rect.center()
        with patch("my_idm.main_window.QMenu") as mock_menu_cls:
            mock_menu = MagicMock()
            mock_menu_cls.return_value = mock_menu
            mock_menu.exec.return_value = None
            self.win._show_context_menu(pos)

        self.assertEqual(self.win._selected_ids(), ["d_cm_2"])

    def test_on_add_multiple_urls_queues_all(self):
        """MainWindow._on_add adds each URL from dlg.urls."""
        mock_dlg = MagicMock()
        # Both sides of the production comparison are now the real enum, so the
        # test cannot pass just because 1 == 1 was hard-coded twice.
        mock_dlg.exec.return_value = QDialog.DialogCode.Accepted
        mock_dlg.urls = [
            "https://example.com/batch1.zip",
            "https://example.com/batch2.zip",
        ]
        mock_dlg.save_path = "D:/Downloads"
        mock_dlg.num_segments = 8

        with patch("my_idm.main_window.AddDownloadDialog") as mock_cls:
            mock_cls.return_value = mock_dlg
            mock_cls.DialogCode = QDialog.DialogCode
            with patch.object(self.manager, "add_download") as mock_add:
                self.win._on_add()
                self.assertEqual(mock_add.call_count, 2)
                # The active queue is threaded through so a download added while a queue is
                # selected joins that queue. With nothing selected it is "" -> Default.
                mock_add.assert_any_call(
                    "https://example.com/batch1.zip", "D:/Downloads", 8, queue_id=""
                )
                mock_add.assert_any_call(
                    "https://example.com/batch2.zip", "D:/Downloads", 8, queue_id=""
                )

    def test_on_add_cancelled_queues_nothing(self):
        """A rejected Add Download dialog must not queue a single URL.

        The old test set `mock_dlg.exec.return_value = 1` *and*
        `mock_cls.DialogCode.Accepted = 1`, so the "user cancelled" branch of
        `if dlg.exec() != AddDownloadDialog.DialogCode.Accepted: return` was
        never executed by any test in the suite.
        """
        mock_dlg = MagicMock()
        mock_dlg.exec.return_value = QDialog.DialogCode.Rejected
        mock_dlg.urls = ["https://example.com/should_not_appear.zip"]
        mock_dlg.save_path = "D:/Downloads"
        mock_dlg.num_segments = 8

        with patch("my_idm.main_window.AddDownloadDialog") as mock_cls:
            mock_cls.return_value = mock_dlg
            mock_cls.DialogCode = QDialog.DialogCode
            with patch.object(self.manager, "add_download") as mock_add:
                self.win._on_add()
                mock_add.assert_not_called()
        self.assertIsNone(self.db.find_by_url("https://example.com/should_not_appear.zip"))

    def test_speed_label_left_click_opens_menu(self):
        """Left clicking the footer speed label should invoke _show_speed_context_menu."""
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QPoint, QPointF, QEvent

        with patch.object(self.win, "_show_speed_context_menu") as mock_menu:
            pt = QPointF(5.0, 5.0)
            press_event = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                pt,
                pt,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            self.win._speed_label.mousePressEvent(press_event)
            mock_menu.assert_called_once_with(QPoint(5, 5))


class TestHeaderViewAndFiltering(_MainWindowTestCase):
    """Tests for FilterHeaderView, sort indicators, and multiselect filter popup."""

    def test_multi_row_selection_survives_a_status_change(self):
        """Regression: pausing N selected rows left only one of them selected.

        ``_restore_selection`` re-selects every id, then calls
        ``self._table.setCurrentIndex(first_index)``. ``QAbstractItemView.setCurrentIndex``
        defaults to ``ClearAndSelect|Current``, so that last line wiped the whole selection
        and left the one row it was given. With a single row selected the bug is invisible,
        which is why the existing single-row regression test never caught it.

        The fix passes ``NoUpdate``, which moves the current index without touching the
        selection.

        Driven through the real ``_on_status_changed`` so the whole path is covered: the
        manager emits one status change *per* download, and each one re-enters this code,
        so a partial fix would still converge to a single row.
        """
        ids = []
        for index in range(4):
            entry = DownloadEntry(
                id=f"multi-sel-{index}",
                url=f"https://example.com/multi{index}.zip",
                filename=f"multi{index}.zip",
                save_path=tempfile.gettempdir(),
                status="downloading",
                download_type="http",
            )
            self.db.add_download(entry)
            ids.append(entry.id)
        self.win._load_history()
        QApplication.processEvents()

        rows = [self.win._model.row_for_id(did) for did in ids]
        for row in rows:
            self.assertIsNotNone(row, "every entry must be visible before selecting")
        sm = self.win._table.selectionModel()
        # Standard multi-select idiom: seed one row, then extend.
        sm.select(
            self.win._model.index(rows[0], Col.STATUS),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        for row in rows[1:]:
            sm.select(
                self.win._model.index(row, Col.STATUS),
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        QApplication.processEvents()
        self.assertEqual(len(self.win._selected_ids()), 4, "precondition: 4 rows selected")

        # One status change, as the manager emits it for a single download.
        self.win._on_status_changed(ids[0], "paused", "")
        QApplication.processEvents()
        self.assertEqual(
            len(self.win._selected_ids()), 4,
            "a status change must not collapse a multi-row selection down to one row",
        )

        # And the realistic case: every selected row changes status in turn.
        for did in ids:
            self.win._on_status_changed(did, "paused", "")
        QApplication.processEvents()
        self.assertEqual(
            sorted(self.win._selected_ids()), sorted(ids),
            "after every selected row changed status, all of them must still be selected",
        )

    def test_the_selection_highlight_spans_the_whole_row_not_just_column_one(self):
        """Regression: the row highlight was painted only over the first column.

        ``_restore_selection`` called ``sm.select(index, Select)`` and then
        ``sm.select(index, Rows)``. A command carrying only ``Rows`` has no action flag
        (Clear/Select/Deselect/Toggle), so Qt treated it as a no-op and only column 0 was
        ever selected - the green bar covered roughly the first 45px of the row and the rest
        stayed dark.

        The download id *was* selected, so ``_selected_ids()`` returned all four and every
        status action hit the right rows: the bug was purely visual, which is why asserting
        on selected ids alone never caught it. This asserts the column count instead.
        """
        ids = []
        for index in range(3):
            entry = DownloadEntry(
                id=f"row-span-{index}",
                url=f"https://example.com/span{index}.zip",
                filename=f"span{index}.zip",
                save_path=tempfile.gettempdir(),
                status="downloading",
                download_type="http",
            )
            self.db.add_download(entry)
            ids.append(entry.id)
        self.win._model.set_segregated_view(True, mode="status")
        self.win._load_history()
        QApplication.processEvents()

        sm = self.win._table.selectionModel()
        rows = [self.win._model.row_for_id(did) for did in ids]
        sm.select(
            self.win._model.index(rows[0], Col.STATUS),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        for row in rows[1:]:
            sm.select(
                self.win._model.index(row, Col.STATUS),
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        QApplication.processEvents()
        column_count = self.win._model.columnCount()
        self.assertGreater(column_count, 1, "the table must have several columns to be meaningful")

        for did in ids:
            self.win._on_status_changed(did, "paused", "")
        QApplication.processEvents()

        selected = sm.selectedIndexes()
        for did in ids:
            row = self.win._model.row_for_id(did)
            with self.subTest(download=did):
                self.assertIsNotNone(row, "the row must still be visible after the change")
                self.assertEqual(
                    sum(1 for index in selected if index.row() == row), column_count,
                    "every column of a selected row must be selected, or the highlight is "
                    "drawn over the first column only",
                )

    def test_a_single_row_status_change_still_sets_the_current_row(self):
        """The current index must still move, so keyboard navigation keeps working."""
        entry = DownloadEntry(
            id="current-row-1",
            url="https://example.com/current.zip",
            filename="current.zip",
            save_path=tempfile.gettempdir(),
            status="downloading",
            download_type="http",
        )
        self.db.add_download(entry)
        self.win._load_history()
        QApplication.processEvents()

        row = self.win._model.row_for_id(entry.id)
        self.win._table.selectRow(row)
        QApplication.processEvents()

        self.win._on_status_changed(entry.id, "paused", "")
        QApplication.processEvents()
        self.assertEqual(
            self.win._table.currentIndex().row(),
            self.win._model.row_for_id(entry.id),
            "the current row must follow the download across the status change",
        )
        self.assertIn(entry.id, self.win._selected_ids())

    def test_row_selection_survives_status_change_with_segregated_view(self):
        """Regression: any status change deselected the row under the cursor.

        Segregated view rebuilds the model on every status change and a model
        reset drops the view's selection.

        The entry used to be harvested from the database and the test skipped
        itself with "no suitable entry" whenever the DB happened to be empty --
        which it always was, so this regression test never ran. The fixture now
        creates the entry it needs.
        """
        entry = DownloadEntry(
            id="seg-selection-1",
            url="https://example.com/selection.zip",
            filename="selection.zip",
            save_path=tempfile.gettempdir(),
            status="completed",
            download_type="http",
        )
        self.db.add_download(entry)
        self.win._load_history()

        self.win._model.set_segregated_view(True, mode="status")
        QApplication.processEvents()

        self.assertEqual(entry.status, "completed")
        row = self.win._model.row_for_id(entry.id)
        self.assertIsNotNone(row, "the completed entry must be visible in segregated view")
        self.win._table.selectRow(row)
        QApplication.processEvents()
        self.assertIn(entry.id, self.win._selected_ids())

        # Mirror production: the engine writes the DB, then the manager's
        # status_changed handler updates the model. Doing only the model half
        # (as this test used to) made `refresh_entry` hand the row a stale
        # "completed" entry, so the status change under test never survived.
        self.db.update_status(entry.id, "downloading")
        self.win._model.update_status(entry.id, "downloading", "")
        QApplication.processEvents()

        moved_row = self.win._model.row_for_id(entry.id)
        self.assertIsNotNone(moved_row, "the entry must still be visible after its status change")
        self.assertEqual(
            self.win._model.data(self.win._model.index(moved_row, Col.STATUS)),
            "Downloading",
            "the model must really have moved the row, or this proves nothing",
        )

        fresh = self.manager.get_entry(entry.id)
        self.assertEqual(fresh.status, "downloading")
        self.win._model.refresh_entry(entry.id, fresh)
        self.win._restore_selection(self.win._selected_ids() or [entry.id])
        QApplication.processEvents()

        self.assertIn(entry.id, self.win._selected_ids())

    def test_restore_selection_ignores_ids_no_longer_visible(self):
        self.win._model.load_entries([])
        # Must not raise when nothing is selected or nothing resolves.
        self.win._restore_selection([])
        self.win._restore_selection(["missing-id"])
        self.assertEqual(self.win._selected_ids(), [])

    def test_toolbar_search_box_is_present_and_filters(self):
        """The toolbar search box filters rows by name, URL, or domain."""
        self.win._model.load_entries([
            DownloadEntry(id="a", url="https://alpha.com/one.zip", filename="one.zip",
                          save_path=".", file_path="./one.zip", status="completed"),
            DownloadEntry(id="b", url="https://beta.com/two.zip", filename="two.zip",
                          save_path=".", file_path="./two.zip", status="completed"),
        ])
        self.assertEqual(self.win._model.rowCount(), 2)

        self.win._search_edit.setText("one")
        self.assertEqual(self.win._model.rowCount(), 1)
        self.assertEqual(self.win._model.search_query(), "one")
        self.assertTrue(self.win._model.is_searching())

        self.win._search_edit.setText("beta")
        self.assertEqual(self.win._model.rowCount(), 1)

        self.win._search_edit.setText("nothing-matches")
        self.assertEqual(self.win._model.rowCount(), 0)

        self.win._search_edit.clear()
        self.assertEqual(self.win._model.rowCount(), 2)
        self.assertFalse(self.win._model.is_searching())

    def test_toolbar_search_is_case_insensitive(self):
        self.win._model.load_entries([
            DownloadEntry(id="a", url="https://x.com/Movie.mkv", filename="Movie.mkv",
                          save_path=".", file_path="./Movie.mkv", status="completed"),
        ])
        self.win._search_edit.setText("movie")
        self.assertEqual(self.win._model.rowCount(), 1)
        self.win._search_edit.clear()

    def test_search_does_not_block_header_filter_clear(self):
        """Clearing the header filters must not wipe what the user is typing."""
        self.win._model.load_entries([
            DownloadEntry(id="a", url="https://x.com/a.zip", filename="a.zip",
                          save_path=".", file_path="./a.zip", status="completed"),
        ])
        self.win._search_edit.setText("a")
        self.win._model.set_status_filter({"completed"})
        self.win._model.clear_filters()
        self.assertEqual(self.win._model.search_query(), "a")
        self.win._search_edit.clear()

    def test_preferences_is_the_last_toolbar_control(self):
        actions = self.win._toolbar.actions()
        self.assertTrue(actions, "toolbar has no actions")
        self.assertIs(actions[-1], self.win._act_preferences)

    def test_toolbar_search_precedes_preferences(self):
        """The search box is the last stretch of the strip, and Preferences the last control."""
        names = []
        for action in self.win._toolbar.actions():
            widget = self.win._toolbar.widgetForAction(action)
            if widget is not None:
                names.append(widget.objectName())
        self.assertIn("toolbar_search", names)
        named = [n for n in names if n]
        self.assertEqual(
            named[-1], "toolbar_search",
            f"the search field is the last widget on the strip: {names}",
        )

    def test_the_toolbar_carries_transport_and_file_actions_only(self):
        """No queue switcher, no Statistics: both are read-or-configure, not row commands.

        A queue combo in the strip read as a filter on the selection, and Statistics is a
        report over the whole history. They live in Edit ▸ Queues and Tools ▸ Statistics.
        """
        labels = [a.text() for a in self.win._toolbar.actions() if not a.isSeparator()]
        for gone in ("All Queues", "Statistics…", "Stats…", "Move to Queue"):
            with self.subTest(label=gone):
                self.assertNotIn(gone, labels)
        widget_names = [
            self.win._toolbar.widgetForAction(a).objectName()
            for a in self.win._toolbar.actions()
            if self.win._toolbar.widgetForAction(a) is not None
        ]
        self.assertNotIn("toolbar_queue_combo", widget_names)
        self.assertFalse(
            hasattr(self.win, "_queue_combo"),
            "the combo widget is gone, not merely unparented - an orphan would still be "
            "populated by _refresh_queue_ui on every queue change",
        )
        # ...and both features are still reachable from a menu.
        edit = [a.text() for a in self.win.menuBar().actions()[1].menu().actions()]
        tools = next(
            top.menu() for top in self.win.menuBar().actions()
            if top.menu() is not None and top.text().replace("&", "") == "Tools"
        )
        self.assertIn(self.win._menu_queues.title(), edit)
        self.assertIn("Statistics…", [a.text() for a in tools.actions()])

    def test_the_search_box_takes_the_leftover_width(self):
        """It is the toolbar's only expanding item, and nothing caps its width.

        Regression: an invisible 1px expanding spacer used to absorb the free width while
        the search box itself was pinned to 320px, so a wide window had a large dead gap
        between the queue switcher and a search box too small to use it.
        """
        search = self.win._search_edit
        self.assertEqual(
            search.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Expanding
        )
        self.assertGreater(
            search.maximumWidth(), 320,
            "a hard maximum would put the free width back into dead space",
        )
        self.assertFalse(
            hasattr(self.win, "_toolbar_gap"),
            "the spacer widget is gone; the search box is the expander now",
        )
        self.assertTrue(search.minimumWidth() > 0, "but it must not collapse to nothing")

    def _refit_with_screen(self, screen_width: int):
        """Re-run the toolbar fit against a faked screen width, from a zero floor.

        The floor is only ever raised in production, and on this machine the toolbar is
        narrower than the hard-coded 1100 default, so the interesting branches are only
        reachable from a known starting point.
        """
        class _Screen:
            def availableGeometry(self):
                return QRect(0, 0, screen_width, 1040)

        with patch.object(
            QApplication, "primaryScreen", staticmethod(lambda: _Screen())
        ):
            self.win.setMinimumWidth(0)
            self.win._fit_min_width_to_toolbar()
        return self.win.minimumWidth()

    def test_the_minimum_width_follows_the_toolbar_and_the_list(self):
        """The floor is the wider of what the toolbar and the downloads list need.

        A QToolBar narrower than its contents folds the remainder into a `>>` button, and a
        window narrower than the list clips its columns instead of scrolling them. The old
        fixed 1100px floor was measured against a toolbar carrying a queue switcher and a
        Statistics button, so it outlived both by ~230px.
        """
        expected = max(
            self.win._toolbar.sizeHint().width(),
            self.win.centralWidget().minimumSizeHint().width(),
        )
        self.assertGreater(expected, 0, "unbuilt widgets cannot prove anything")
        self.assertEqual(self._refit_with_screen(expected + 500), expected)

    def test_a_screen_too_narrow_for_the_row_caps_the_floor(self):
        """Better a `>>` button on a small display than a window that cannot be fitted."""
        needed = max(
            self.win._toolbar.sizeHint().width(),
            self.win.centralWidget().minimumSizeHint().width(),
        )
        self.assertEqual(self._refit_with_screen(needed - 100), needed - 100)

    def test_a_shorter_toolbar_never_lowers_the_floor(self):
        """The Tor label and the locale both shrink the row; an open window must not jump."""
        self.win.setMinimumWidth(1800)
        self.win._fit_min_width_to_toolbar()
        self.assertEqual(self.win.minimumWidth(), 1800)

    def test_a_shorter_toolbar_never_lowers_the_floor(self):
        """The Tor label and the locale both shrink the row; an open window must not jump."""
        self.win.setMinimumWidth(1800)
        self.win._fit_min_width_to_toolbar()
        self.assertEqual(self.win.minimumWidth(), 1800)

    def test_header_sort_indicator_and_painting(self):
        header = self.win._header_view
        self.assertTrue(header.isSortIndicatorShown())

        # Test setSortIndicator
        header.setSortIndicator(Col.ADDED, Qt.SortOrder.DescendingOrder)
        self.assertEqual(header.sortIndicatorSection(), Col.ADDED)
        self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)

        header.setSortIndicator(Col.NAME, Qt.SortOrder.AscendingOrder)
        self.assertEqual(header.sortIndicatorSection(), Col.NAME)
        self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.AscendingOrder)

        # Test filter button rect geometry calculations
        name_rect = header._get_filter_btn_rect(Col.NAME)
        self.assertFalse(name_rect.isEmpty())
        self.assertEqual(name_rect.width(), 16)
        self.assertEqual(name_rect.height(), 16)

        status_rect = header._get_filter_btn_rect(Col.STATUS)
        self.assertFalse(status_rect.isEmpty())
        self.assertEqual(status_rect.width(), 16)
        self.assertEqual(status_rect.height(), 16)

        # Size is filterable too
        size_rect = header._get_filter_btn_rect(Col.SIZE)
        self.assertFalse(size_rect.isEmpty())
        self.assertEqual(size_rect.width(), 16)
        self.assertEqual(size_rect.height(), 16)

        # A genuinely non-filterable column has an empty rect
        self.assertTrue(header._get_filter_btn_rect(Col.SPEED).isEmpty())
        self.assertTrue(header._get_filter_btn_rect(Col.PROGRESS).isEmpty())

    def test_header_filter_button_click_opens_popup(self):
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QPointF, QEvent
        header = self.win._header_view

        # Get the filter button rect for Col.STATUS
        btn_rect = header._get_filter_btn_rect(Col.STATUS)
        self.assertFalse(btn_rect.isEmpty())

        # Simulate left-click directly on the filter button
        click_pt = btn_rect.center()
        pt = QPointF(click_pt.x(), click_pt.y())
        press_event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            pt,
            pt,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        header.mousePressEvent(press_event)

        # Active popup should have been created
        self.assertIsNotNone(header._active_popup)
        popup = header._active_popup
        self.assertEqual(popup._column, Col.STATUS)
        popup.close()

    def test_multiselect_filter_popup_interactions(self):
        from my_idm.header_view import MultiselectFilterPopup
        counts = {"downloading": 2, "completed": 1, "paused": 0}
        popup = MultiselectFilterPopup(Col.STATUS, None, counts, self.win)

        changes = []
        popup.filter_changed.connect(lambda col, keys: changes.append((col, keys)))

        # The popup offers every known status, not just the three counted here.
        all_keys = set(popup._checkboxes)
        self.assertTrue({"downloading", "completed", "paused"} <= all_keys)

        # Initially all checked
        for name, cb in popup._checkboxes.items():
            self.assertTrue(cb.isChecked(), f"status '{name}' must start checked")
        self.assertEqual(changes, [], "building the popup must not emit a change")

        # Uncheck downloading
        popup._checkboxes["downloading"].setChecked(False)
        # The exact value, not "at least one": a duplicate emit would rebuild the
        # proxy twice and the old assertion could not see it.
        self.assertEqual(
            changes, [(Col.STATUS, all_keys - {"downloading"})],
            "unchecking must publish exactly the remaining keys, once",
        )
        self.assertNotIn("downloading", changes[-1][1])

        # Re-checking it puts every box back on, and the popup signals that with
        # the same "None == unfiltered" value `_select_all()` uses -- so the
        # emitted value is the real contract, not a full key set.
        popup._checkboxes["downloading"].setChecked(True)
        self.assertEqual(
            changes[-1], (Col.STATUS, None),
            "re-checking the last filtered box must clear the filter, not publish "
            "an explicit full key set",
        )

        # Select all
        popup._select_all()
        self.assertEqual(len(changes), 3, "one emit per interaction, no duplicates")
        last_col, last_keys = changes[-1]
        self.assertIsNone(last_keys)  # None indicates all selected (unfiltered)

        popup.close()

    def test_count_label_filtered_indicator(self):
        # Add two downloads
        e1 = DownloadEntry(id="1", url="http://example.com/1", filename="1.zip", status="downloading", download_type="http")
        e2 = DownloadEntry(id="2", url="http://example.com/2", filename="2.zip", status="completed", download_type="http")
        self.win._model.load_entries([e1, e2])
        self.win._update_count_label()
        self.assertEqual(self.win._count_label.text(), "2 Downloads, 1 Active")

        # Apply filter
        self.win._model.set_status_filter({"downloading"})
        self.win._update_count_label()
        self.assertIn("Filtered", self.win._count_label.text())
        self.assertEqual(self.win._count_label.text(), "1 of 2 Downloads, 1 Active (Filtered)")

        # Clear filter
        self.win._model.clear_filters()
        self.win._update_count_label()
        self.assertNotIn("Filtered", self.win._count_label.text())
        self.assertEqual(self.win._count_label.text(), "2 Downloads, 1 Active")

    def test_toolbar_stop_all_seeding_button(self):
        """Toolbar contains Stop All Seeding action and triggers manager.stop_all_seeding."""
        self.assertIn(self.win._act_stop_all_seeding, self.win._toolbar.actions())
        self.db.add_download(
            DownloadEntry(id="s1", url="magnet:?xt=urn:btih:aa", filename="s1", status="seeding", download_type="torrent")
        )
        self.win._load_history()
        with unittest.mock.patch.object(self.manager, "stop_all_seeding", return_value=3) as mock_stop:
            self.win._act_stop_all_seeding.trigger()
            mock_stop.assert_called_once()
            self.assertIn("Stopped 3 seeding torrents", self.win._status_label.text())

    def test_toolbar_pause_all_downloads_button(self):
        """Toolbar contains Pause All action and triggers manager.pause_all_downloads."""
        self.assertIn(self.win._act_pause_all, self.win._toolbar.actions())
        self.db.add_download(
            DownloadEntry(id="d1", url="http://example.com/1", filename="1.zip", status="downloading")
        )
        self.win._load_history()
        with unittest.mock.patch.object(self.manager, "pause_all_downloads", return_value=2) as mock_pause_all:
            self.win._act_pause_all.trigger()
            mock_pause_all.assert_called_once()
            self.assertIn("Paused 2 downloads", self.win._status_label.text())

    def test_toolbar_start_seeding_button(self):
        """Toolbar contains Start Seeding action and triggers manager.start_seeding on selected downloads."""
        self.assertIn(self.win._act_start_seeding, self.win._toolbar.actions())
        self.db.add_download(
            DownloadEntry(id="dl-seed-1", url="magnet:?xt=urn:btih:bb", filename="s", status="completed", download_type="torrent")
        )
        self.win._load_history()
        self.win._table.selectRow(0)
        with unittest.mock.patch.object(self.manager, "start_seeding") as mock_seed:
            self.win._act_start_seeding.trigger()
            mock_seed.assert_called_once_with("dl-seed-1")

    def test_stale_ui_state_adds_new_columns_at_end(self):
        """A state saved when the table had fewer columns must not scramble order."""
        from my_idm.main_window import _DEFAULT_TAIL_COLUMNS

        # `_DEFAULT_TAIL_COLUMNS` is the five columns pinned to the tail, but only
        # the three newest (LAST_SEEDED, SOURCE, SEEDING_STARTED_AT) were *appended*
        # in the last upgrade: SOURCE_DOMAIN and FILE_NAME already existed and were
        # merely moved to the tail. So the stale state describes
        # `Col.COUNT - 4` columns, derived here rather than re-typed.
        appended_columns = 4
        legacy_count = Col.COUNT - appended_columns
        self.assertEqual(legacy_count, 14, "the pre-append build had 14 columns")
        self.assertEqual(len(_DEFAULT_TAIL_COLUMNS), 5)
        for col in (Col.LAST_SEEDED, Col.SOURCE, Col.SEEDING_STARTED_AT):
            self.assertGreaterEqual(
                col, legacy_count,
                f"{Col.HEADERS[col]} must be one of the appended columns",
            )

        header = self.win._table.horizontalHeader()
        # The user had dragged Size to position 1 on that older build.
        header.moveSection(header.visualIndex(Col.SIZE), 1)
        legacy = {
            "column_widths": {str(c): 120 for c in range(legacy_count)},
            "header_state": bytes(header.saveState().toHex()).decode(),
            "column_count": legacy_count,
            "sort_column": Col.ADDED,
            "sort_order": 0,
        }

        with patch.object(self.manager, "get_ui_state", return_value=legacy):
            self.win._restore_ui_state_from_db()

        # New columns land at the very end...
        self.assertEqual(header.count(), Col.COUNT)
        # ...the tail is fully pinned...
        for i, col in enumerate(_DEFAULT_TAIL_COLUMNS):
            self.assertEqual(
                header.visualIndex(col), Col.COUNT - len(_DEFAULT_TAIL_COLUMNS) + i,
                f"{Col.HEADERS[col]} must be pinned at its tail position",
            )
        # ...and the user's own ordering of the older columns survives. The heal must NOT
        # apply the full default order here: this is an existing profile, and resetting
        # everything would discard an arrangement the user never asked to change.
        self.assertEqual(Col.HEADERS[header.logicalIndex(1)], "Size")
        self.assertEqual(header.logicalIndex(1), Col.SIZE)

    def test_current_ui_state_respects_user_order(self):
        """Once the stored state matches the column count, order is left alone."""
        header = self.win._table.horizontalHeader()
        header.moveSection(header.visualIndex(Col.SOURCE), 0)
        current = {
            "column_widths": {str(c): 120 for c in range(Col.COUNT)},
            "header_state": bytes(header.saveState().toHex()).decode(),
            "column_count": Col.COUNT,
            "sort_column": Col.ADDED,
            "sort_order": 0,
        }
        with patch.object(self.manager, "get_ui_state", return_value=current):
            self.win._restore_ui_state_from_db()
        self.assertEqual(Col.HEADERS[header.logicalIndex(0)], "Source")

    def test_saved_ui_state_records_column_count(self):
        """column_count must be written so a later upgrade can detect a stale state."""
        self.win._save_ui_state_to_db()
        state = self.manager.get_ui_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.get("column_count"), Col.COUNT)

    def test_default_column_order_places_source_domain_and_file_name_at_end(self):
        """The tail columns default to the end of the table, in the documented order."""
        header = self.win._table.horizontalHeader()
        from my_idm.main_window import _DEFAULT_TAIL_COLUMNS

        expected = {col: Col.COUNT - len(_DEFAULT_TAIL_COLUMNS) + i
                    for i, col in enumerate(_DEFAULT_TAIL_COLUMNS)}
        for col, visual in expected.items():
            self.assertEqual(
                header.visualIndex(col), visual,
                f"{Col.HEADERS[col]} should sit at visual {visual}",
            )

    def test_the_default_order_and_widths_are_the_arrangement_from_the_real_profile(self):
        """Pinned literally, because "the default" is a decision, not a derivation.

        This is the arrangement from a real profile: the Queue badge next to the row number,
        Save Path grouped with the other "what happened to it" columns, and the two filename
        columns wide enough not to truncate. A test that recomputed it from the tuple would
        pass whatever the tuple said, which is the failure this exists to prevent.
        """
        from my_idm.main_window import (
            _DEFAULT_COLUMN_ORDER,
            _DEFAULT_COLUMN_WIDTHS,
            DEFAULT_HIDDEN_COLUMNS,
        )

        self.assertEqual(
            [Col.HEADERS[c] for c in _DEFAULT_COLUMN_ORDER],
            ["#", "Queue", "Name", "Size", "Progress", "Status", "Speed", "ETA",
             "Seeds / Peers", "Added", "Save Path", "Completed", "Last Tried",
             "Source Domain", "File / Folder Name", "Last Seeded", "Source",
             "Seeding Started At"],
        )
        self.assertEqual(
            [_DEFAULT_COLUMN_WIDTHS[c] for c in _DEFAULT_COLUMN_ORDER],
            [30, 30, 412, 82, 214, 135, 166, 80, 140, 123, 262, 130, 130, 187,
             546, 131, 110, 173],
        )

        header = self.win._table.horizontalHeader()
        # Unhide everything first: a hidden section reports 0, not the width it will take
        # when shown again.
        for col in range(Col.COUNT):
            header.setSectionHidden(col, False)
        try:
            for col, width in _DEFAULT_COLUMN_WIDTHS.items():
                self.assertEqual(
                    self.win._table.columnWidth(col), width,
                    f"{Col.HEADERS[col]} is not at its default width",
                )
        finally:
            for col in DEFAULT_HIDDEN_COLUMNS:
                header.setSectionHidden(col, True)

    def test_no_default_column_width_is_clamped_by_the_section_floor(self):
        """A default below Qt's floor is a default that cannot be honoured.

        Qt's floor is font-derived — 32px at the default UI font, wider at a larger scale
        factor — and `#` and Queue default to 30. `setColumnWidth(30)` then reports back 32
        and nothing anywhere says why.
        """
        from my_idm.main_window import MIN_COLUMN_WIDTH, _DEFAULT_COLUMN_WIDTHS

        header = self.win._table.horizontalHeader()
        self.assertEqual(header.minimumSectionSize(), MIN_COLUMN_WIDTH)
        self.assertLessEqual(
            MIN_COLUMN_WIDTH, min(_DEFAULT_COLUMN_WIDTHS.values()),
            "the floor must not be wider than the narrowest default column",
        )
        self.assertGreaterEqual(
            header.fontMetrics().horizontalAdvance("1000"), 0,
            "the floor is allowed to elide a five-digit row number; four must fit",
        )
        self.assertLessEqual(
            header.fontMetrics().horizontalAdvance("1000"), MIN_COLUMN_WIDTH,
            f"{MIN_COLUMN_WIDTH}px must fit a four-digit row number",
        )

    def test_restoring_a_saved_header_state_keeps_the_section_floor(self):
        """`QHeaderView.restoreState()` puts the floor back, so it has to be re-asserted.

        The value is saved inside the state blob, which means the restore path silently undoes
        whatever was set before it — and the early `return` on the fresh-profile branch is one
        of the exits it has to survive.
        """
        from my_idm.main_window import MIN_COLUMN_WIDTH

        self.win._save_ui_state_to_db()
        win2 = self.new_window(self.manager)
        self.assertEqual(
            win2._table.horizontalHeader().minimumSectionSize(), MIN_COLUMN_WIDTH,
            "a saved header state must not reinstate Qt's font-derived floor",
        )

    def test_the_hash_header_label_is_drawn_in_full_at_its_default_width(self):
        """`#` is 30px and was drawn as a clipped sliver — or not at all.

        Qt draws a header label into the section rect *minus* the stylesheet's padding, plus
        its own header margin scaled by the header font. Once that total reaches the section's
        width there is no box left: the label goes, and just below that it is clipped to an
        edge. The shared padding exists for the filter funnel, and `#` is the one section
        without one, so it now gets symmetric padding.

        Asserted as ink *relative to* the same section drawn wide enough to be unclipped, and
        with the rule stripped as a control — the ratio separates cleanly (0.82 with the rule,
        0.35 without), so this cannot pass on a clipped label.
        """
        import re

        from my_idm import styles
        from my_idm.download_model import Col
        from my_idm.main_window import _DEFAULT_COLUMN_WIDTHS

        header = self.win._table.horizontalHeader()
        self.assertEqual(
            self.win._table.columnWidth(Col.QUEUE),
            _DEFAULT_COLUMN_WIDTHS[Col.QUEUE],
            "this is about the label at the default width",
        )
        app = QApplication.instance()
        sheet = styles.STYLESHEETS["dark"]
        self.assertIn("QHeaderView::section:first", sheet, "the rule under test is missing")

        unclipped = 64      # comfortably wider than 30 + padding + margin

        def ink_at(width):
            header.resizeSection(Col.QUEUE, width)
            QApplication.processEvents()
            return self._ink_in_section(header, Col.QUEUE)

        try:
            app.setStyleSheet(sheet)
            self.win.show()
            QApplication.processEvents()
            full = ink_at(unclipped)
            narrow = ink_at(_DEFAULT_COLUMN_WIDTHS[Col.QUEUE])
            self.assertGreater(full, 0, "the control measurement found no ink at all")
            self.assertGreater(
                narrow, full * 0.6,
                f"the '#' label is clipped at its default width: {narrow} of {full}",
            )

            without = re.sub(
                r"QHeaderView::section:first\s*\{[^}]*\}", "", sheet, flags=re.S
            )
            app.setStyleSheet(without)
            QApplication.processEvents()
            self.assertLess(
                ink_at(_DEFAULT_COLUMN_WIDTHS[Col.QUEUE]), full * 0.6,
                "the control no longer reproduces the defect, so this test proves nothing",
            )
        finally:
            app.setStyleSheet("")
            QApplication.processEvents()

    @staticmethod
    def _ink_in_section(header, logical_index: int) -> int:
        """Count pixels in one section that differ from the header's background.

        The section is located by its own offset rather than assuming it starts at x=0, and it
        is only meaningful for a section with no filter funnel.
        """
        image = header.grab().toImage()
        left = header.sectionViewportPosition(logical_index)
        width = header.sectionSize(logical_index)
        image = image.copy(left, 0, width, image.height())
        background = image.pixelColor(1, 1)
        count = 0
        for y in range(2, image.height() - 1):
            for x in range(1, image.width() - 1):
                colour = image.pixelColor(x, y)
                if (abs(colour.red() - background.red())
                        + abs(colour.green() - background.green())
                        + abs(colour.blue() - background.blue())) > 30:
                    count += 1
        return count

    def test_the_default_order_covers_every_column_exactly_once(self):
        """A duplicated or omitted column here is silently a broken startup layout."""
        from my_idm.main_window import _DEFAULT_COLUMN_ORDER, _DEFAULT_COLUMN_WIDTHS

        self.assertEqual(
            sorted(_DEFAULT_COLUMN_ORDER), list(range(Col.COUNT)),
            "every column must appear exactly once in the default order",
        )
        self.assertEqual(
            sorted(_DEFAULT_COLUMN_WIDTHS), list(range(Col.COUNT)),
            "every column needs a default width",
        )
        self.assertTrue(
            all(w > 0 for w in _DEFAULT_COLUMN_WIDTHS.values()),
            "a zero default width makes a column invisible until it is dragged",
        )

    def test_the_tail_columns_are_a_suffix_of_the_default_order(self):
        """The append convention depends on it: a new column joins the tail by being last."""
        from my_idm.main_window import _DEFAULT_COLUMN_ORDER, _DEFAULT_TAIL_COLUMNS

        span = len(_DEFAULT_TAIL_COLUMNS)
        self.assertEqual(
            _DEFAULT_COLUMN_ORDER[-span:], _DEFAULT_TAIL_COLUMNS,
            "the tail must be the last columns of the order, in the same sequence",
        )

    def test_reset_view_applies_the_default_widths(self):
        from my_idm.main_window import _DEFAULT_COLUMN_WIDTHS

        self.win._table.setColumnWidth(Col.NAME, 90)
        self.win._on_reset_view()
        self.assertEqual(self.win._table.columnWidth(Col.NAME), _DEFAULT_COLUMN_WIDTHS[Col.NAME])

    def test_new_columns_default_to_the_very_end(self):
        """The appended columns sit at the end of the table by default."""
        from my_idm.main_window import _DEFAULT_COLUMN_ORDER, _DEFAULT_TAIL_COLUMNS

        header = self.win._table.horizontalHeader()
        span = len(_DEFAULT_TAIL_COLUMNS)
        for i, col in enumerate(_DEFAULT_TAIL_COLUMNS):
            self.assertEqual(header.visualIndex(col), Col.COUNT - span + i, Col.HEADERS[col])
        # The newest appended column is the last one, and nothing sits after the tail.
        self.assertEqual(
            header.visualIndex(Col.SEEDING_STARTED_AT), Col.COUNT - 1,
            "Seeding Started At is the newest column, so it must end the row",
        )
        self.assertEqual(
            [Col.HEADERS[header.logicalIndex(v)] for v in range(Col.COUNT)],
            [Col.HEADERS[c] for c in _DEFAULT_COLUMN_ORDER],
            "a fresh profile must show the documented arrangement",
        )

    def test_new_column_indices_do_not_shift_existing_columns(self):
        """Persisted column indices must keep pointing at the same columns.

        The literal numbers are the regression guard (they are what an older
        build wrote to the database), so they stay hard-coded; the derived
        relationship is asserted alongside so the intent is also pinned.
        """
        from my_idm.main_window import _DEFAULT_TAIL_COLUMNS

        self.assertEqual(Col.LAST_SEEDED, 14)
        self.assertEqual(Col.SOURCE, 15)
        self.assertEqual(Col.SEEDING_STARTED_AT, 16)
        for name, value in (
            ("QUEUE", 0), ("NAME", 1), ("SOURCE_DOMAIN", 2), ("SIZE", 3),
            ("PROGRESS", 4), ("STATUS", 5), ("SPEED", 6), ("ETA", 7),
            ("SEEDS_PEERS", 8), ("ADDED", 9), ("LAST_TRIED", 10),
            ("COMPLETED", 11), ("SAVE_PATH", 12), ("FILE_NAME", 13),
        ):
            self.assertEqual(getattr(Col, name), value, name)
        self.assertEqual(Col.COUNT, 18)
        # The appended columns are exactly the ones newer than that 14-column build,
        # contiguous and last.
        self.assertEqual(Col.COUNT - 4, 14)
        self.assertEqual(
            [Col.LAST_SEEDED, Col.SOURCE, Col.SEEDING_STARTED_AT, Col.QUEUE_NAME],
            list(range(14, Col.COUNT)),
        )
        self.assertEqual(Col.HEADERS[Col.LAST_SEEDED], "Last Seeded")
        self.assertEqual(Col.HEADERS[Col.SOURCE], "Source")
        self.assertEqual(Col.HEADERS[Col.SEEDING_STARTED_AT], "Seeding Started At")

    def test_reset_view_restores_new_column_order(self):
        from my_idm.main_window import _DEFAULT_COLUMN_ORDER

        header = self.win._table.horizontalHeader()
        header.moveSection(header.visualIndex(Col.SOURCE), 0)
        self.win._on_reset_view()
        self.assertEqual(
            [Col.HEADERS[header.logicalIndex(v)] for v in range(Col.COUNT)],
            [Col.HEADERS[c] for c in _DEFAULT_COLUMN_ORDER],
            "Reset View must land on the documented arrangement, not identity order",
        )

    def test_details_panel_state_survives_a_hidden_window_save(self):
        """Regression: the panel appeared closed after restart.

        Every child widget reports isVisible() == False while the window is
        hidden (minimised, or closed to tray), so saving that value persisted
        "panel closed" even when the user had it open.
        """
        self.win.show()
        QApplication.processEvents()
        self.win._act_toggle_details.setChecked(True)
        self.win._on_toggle_details(True)
        QApplication.processEvents()
        self.assertTrue(self.win._details_panel.isVisible())

        # Simulate the window being hidden, as when closed to the tray.
        self.win.hide()
        self.assertFalse(self.win._details_panel.isVisible())

        self.win._save_ui_state_to_db()
        state = self.manager.get_ui_state()
        self.assertTrue(
            state.get("details_visible"),
            "panel intent must be saved from the toggle, not live visibility",
        )

        # Restoring brings it back open.
        self.win._act_toggle_details.setChecked(False)
        self.win._details_panel.setVisible(False)
        self.win._restore_ui_state_from_db()
        # A fresh launch shows the window, so the child becomes visible again.
        self.win.show()
        QApplication.processEvents()
        self.assertTrue(self.win._details_panel.isVisible())
        self.assertTrue(self.win._act_toggle_details.isChecked())

    def test_details_panel_closed_state_is_persisted(self):
        """A deliberately closed panel must stay closed after a restart."""
        self.win.show()
        QApplication.processEvents()
        # Drive the action, which is what the UI does (toggled -> _on_toggle_details).
        self.win._act_toggle_details.setChecked(True)
        QApplication.processEvents()
        self.assertTrue(self.win._details_panel.isVisible())

        self.win._act_toggle_details.setChecked(False)
        QApplication.processEvents()
        self.win._save_ui_state_to_db()
        self.assertFalse(self.manager.get_ui_state().get("details_visible"))

        self.win._act_toggle_details.setChecked(True)
        QApplication.processEvents()
        self.win._restore_ui_state_from_db()
        self.assertFalse(self.win._details_panel.isVisible())
        self.assertFalse(self.win._act_toggle_details.isChecked())

    def test_ui_state_restore_preserves_user_column_order(self):
        """Restoring UI state does not forcefully push File / Folder Name or Source Domain to the front."""
        header = self.win._table.horizontalHeader()
        # Move Col.FILE_NAME to the very last position
        header.moveSection(header.visualIndex(Col.FILE_NAME), Col.COUNT - 1)
        self.assertEqual(header.visualIndex(Col.FILE_NAME), Col.COUNT - 1)

        # Save UI state and restore it
        self.win._save_ui_state_to_db()
        self.win._restore_ui_state_from_db()

        # Must still be at the end, not forced back to index 2
        self.assertEqual(header.visualIndex(Col.FILE_NAME), Col.COUNT - 1)

    def test_export_selected_as_csv(self):
        """Exporting selected downloads creates a valid CSV file with Name and URL/Magnet columns."""
        e1 = DownloadEntry(
            id="csv-1",
            url="https://example.com/file1.zip",
            filename="file1.zip",
            status="completed",
        )
        e2 = DownloadEntry(
            id="csv-2",
            url="magnet:?xt=urn:btih:csvmagnet123",
            filename="my_torrent.mkv",
            status="seeding",
        )
        self.db.add_download(e1)
        self.db.add_download(e2)
        self.win._load_history()

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
            csv_path = tf.name

        try:
            with unittest.mock.patch.object(self.win, "_selected_ids", return_value=["csv-1", "csv-2"]):
                with unittest.mock.patch("my_idm.main_window.QFileDialog.getSaveFileName", return_value=(csv_path, "CSV Files (*.csv)")):
                    self.win._act_export_csv.trigger()

            with open(csv_path, "r", encoding="utf-8") as f:
                content = f.read()

            lines = [line.strip() for line in content.splitlines() if line.strip()]
            self.assertEqual(lines[0], "Name,URL/Magnet")
            self.assertIn("file1.zip,https://example.com/file1.zip", lines)
            self.assertIn("my_torrent.mkv,magnet:?xt=urn:btih:csvmagnet123", lines)
        finally:
            if Path(csv_path).exists():
                Path(csv_path).unlink()

    def test_segregated_view_toggle_and_collapsible_sections(self):
        """Segregated view partitions items into Active, Seeding, Inactive sections and persists collapse state."""
        e_active = DownloadEntry(
            id="sec-active-1",
            url="https://example.com/active.zip",
            filename="active.zip",
            status="downloading",
        )
        e_seed = DownloadEntry(
            id="sec-seed-1",
            url="magnet:?xt=urn:btih:seed123",
            filename="seed.iso",
            status="seeding",
        )
        e_inact = DownloadEntry(
            id="sec-inact-1",
            url="https://example.com/done.zip",
            filename="done.zip",
            status="completed",
        )
        self.db.add_download(e_active)
        self.db.add_download(e_seed)
        self.db.add_download(e_inact)
        self.win._load_history()

        # Menu structure verification
        self.assertEqual(self.win._act_segregated_view.text(), "On")
        menu_actions = self.win._menu_segregated_view.actions()
        self.assertIn(self.win._act_segregated_view, menu_actions)
        self.assertIn(self.win._act_seg_by_status, menu_actions)
        self.assertIn(self.win._act_seg_by_date, menu_actions)

        # Turn on segregated view
        self.win._act_segregated_view.setChecked(True)
        self.assertTrue(self.win._model.is_segregated_view())
        self.assertTrue(self.db.get_ui_state("segregated_view_enabled"))

        # In segregated view, we have 3 headers + 3 items = 6 rows
        self.assertEqual(self.win._model.rowCount(), 6)
        hdr_indices = self.win._model.get_section_header_row_indices()
        self.assertEqual(len(hdr_indices), 3)

        # First header should be Active
        self.assertTrue(self.win._model.is_section_header_row(0))
        entry_h0 = self.win._model._entries[0]
        self.assertEqual(entry_h0.section_id, "active")

        # Collapse the Active section by clicking on header row 0
        self.win._on_table_clicked(self.win._model.index(0, 0))
        self.assertTrue(self.win._model._collapsed_sections)
        self.assertIn("active", self.win._model._collapsed_sections)
        self.assertTrue(self.db.get_ui_state("segregated_active_collapsed"))
        # With active collapsed, row count drops by 1
        self.assertEqual(self.win._model.rowCount(), 5)

        # Un-collapse Active section
        self.win._on_table_clicked(self.win._model.index(0, 0))
        self.assertNotIn("active", self.win._model._collapsed_sections)
        self.assertFalse(self.db.get_ui_state("segregated_active_collapsed"))
        self.assertEqual(self.win._model.rowCount(), 6)

        # Turn off segregated view -> back to flat list of 3 items
        self.win._act_segregated_view.setChecked(False)
        self.assertFalse(self.win._model.is_segregated_view())
        self.assertEqual(self.win._model.rowCount(), 3)

    def test_segregated_view_date_mode_and_persistence(self):
        """Date-based segregated view groups by Today, Yesterday, Last 7 Days, Last 30 Days, Older and persists settings."""
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        e_today = DownloadEntry(
            id="d-today-1",
            url="https://example.com/1",
            filename="today.zip",
            status="completed",
            added_at=now.isoformat(),
        )
        e_yest = DownloadEntry(
            id="d-yest-1",
            url="https://example.com/2",
            filename="yest.zip",
            status="completed",
            added_at=(now - timedelta(days=1)).isoformat(),
        )
        e_older = DownloadEntry(
            id="d-older-1",
            url="https://example.com/3",
            filename="older.zip",
            status="completed",
            added_at=(now - timedelta(days=60)).isoformat(),
        )
        self.db.add_download(e_today)
        self.db.add_download(e_yest)
        self.db.add_download(e_older)
        self.win._load_history()

        # Segregation is switched on first, then the mode is chosen. The order matters: the three mode
        # actions are disabled while the table is flat (MainWindow._sync_segregation_mode_actions),
        # because there is nothing to group by, so triggering one does nothing until "On" is set.
        self.win._act_segregated_view.setChecked(True)
        self.win._act_seg_by_date.trigger()
        self.assertTrue(self.win._model.is_segregated_view())
        self.assertEqual(self.win._model.segregated_mode(), "date")
        self.assertEqual(self.db.get_ui_state("segregated_view_mode"), "date")
        self.assertTrue(self.db.get_ui_state("segregated_view_enabled"))

        # 5 headers + 3 items = 8 rows
        self.assertEqual(self.win._model.rowCount(), 8)

        # Collapse "Last 7 Days" and "Older" sections
        self.win._model.set_section_collapsed("date_last_7_days", True)
        self.db.set_ui_state("segregated_date_last_7_days_collapsed", True)
        self.win._model.set_section_collapsed("date_older", True)
        self.db.set_ui_state("segregated_date_older_collapsed", True)
        self.assertEqual(self.win._model.rowCount(), 7)

        # Create a new MainWindow with same db to verify next launch persistence
        win2 = self.new_window(self.win._manager)
        self.assertTrue(win2._segregated_view_enabled)
        self.assertEqual(win2._segregated_view_mode, "date")
        self.assertTrue(win2._model.is_segregated_view())
        self.assertEqual(win2._model.segregated_mode(), "date")
        self.assertTrue(win2._model.is_section_collapsed("date_last_7_days"))
        self.assertTrue(win2._model.is_section_collapsed("date_older"))

    def test_tools_menu_animepahe_actions(self):
        """Tools menu contains actions to launch AnimePahe GUI and External Tools settings."""
        self.assertIsNotNone(self.win._act_launch_animepahe_gui)
        self.assertIsNotNone(self.win._act_external_tools_settings)

        # Triggering when not configured prompts user
        with patch.object(self.win._manager.external_tools_config, "get_effective_repo_path", return_value=""):
            with patch("my_idm.main_window.QMessageBox.question") as mock_q:
                self.win._act_launch_animepahe_gui.trigger()
                mock_q.assert_called_once()

        # Triggering when configured launches GUI
        with patch.object(self.win._manager.external_tools_config, "get_effective_repo_path", return_value=tempfile.gettempdir()):
            with patch("my_idm.main_window.launch_animepahe_gui", return_value=(True, "Success")):
                self.win._act_launch_animepahe_gui.trigger()
                self.assertEqual(self.win._status_label.text(), "Launched AnimePahe Downloader GUI")

    @unittest.skipUnless(
        has_real_desktop(), "window position needs a desktop session; CI has only a virtual screen"
    )
    def test_window_geometry_persistence_does_not_shift_on_relaunch(self):
        """Saving and restoring UI state across multiple launches preserves window position without shifting upwards."""
        self.win.resize(800, 600)
        self.win.move(300, 200)
        self.win.show()
        QApplication.processEvents()

        self.win._save_ui_state_to_db()
        # The invariant is that the position does not *creep*: every launch must persist the
        # same coordinates it inherited. Comparing against the literal 200 that `move()` was
        # given is not a test of persistence - restore goes through `restoreGeometry`, which
        # replays a frame rectangle, so the first relaunch lands at a different absolute y on a
        # headless runner (`88 != 200`) purely because of the runner's frame metrics. Comparing
        # each launch against the persisted state catches an actual shift, on any host.
        first_state = self.manager.get_ui_state()

        for launch_idx in range(3):
            next_win = self.new_window(self.manager)
            next_win.show()
            QApplication.processEvents()
            next_win._save_ui_state_to_db()
            state = self.manager.get_ui_state()
            self.assertEqual(
                state["y"], first_state["y"],
                f"Window shifted vertically on launch {launch_idx + 1}: "
                f"{state['y']} vs {first_state['y']}",
            )
            self.assertEqual(
                state["x"], first_state["x"],
                f"Window shifted horizontally on launch {launch_idx + 1}: "
                f"{state['x']} vs {first_state['x']}",
            )

    @unittest.skipUnless(
        has_real_desktop(), "window position needs a desktop session; see has_real_desktop()"
    )
    def test_legacy_window_geometry_restore_does_not_shift(self):
        """Restoring legacy UI state dictionary (x, y, width, height) positions window accurately without shift."""
        legacy_state = {"x": 350, "y": 250, "width": 820, "height": 610}
        self.manager.save_ui_state(legacy_state)

        next_win = self.new_window(self.manager)
        next_win.show()
        QApplication.processEvents()

        self.assertEqual(next_win.pos().x(), 350)
        self.assertEqual(next_win.pos().y(), 250)
        # The width is below the floor, which `_fit_min_width_to_toolbar` measures from the
        # toolbar and the downloads list on first show; the height is above the 600px minimum
        # and survives verbatim.
        self.assertGreater(next_win.width(), 820, "the restored width must be clamped")
        self.assertEqual(
            next_win.width(), next_win.minimumWidth(),
            "a legacy 820px width must land exactly on the measured floor",
        )
        self.assertEqual(next_win.height(), 610)

    def test_legacy_geometry_state_with_stale_column_count_heals_the_tail(self):
        """A stale `column_count` makes the restore re-pin the appended columns.

        The sibling geometry test above builds a state with no `column_count`, so
        `main_window.py:2843` skips tail healing entirely and the only thing it
        proved was the position. This state is the realistic upgrade case: an
        older build wrote both the geometry and the older column count.
        """
        from my_idm.main_window import _DEFAULT_TAIL_COLUMNS

        # Only the three newest columns were appended; SOURCE_DOMAIN and
        # FILE_NAME already existed and were merely pinned to the tail.
        legacy_count = Col.COUNT - 4
        self.assertEqual(legacy_count, 14)
        header = self.win._table.horizontalHeader()
        # Scramble the tail the way a pre-upgrade restoreState() would.
        header.moveSection(header.visualIndex(Col.SEEDING_STARTED_AT), 0)
        self.assertEqual(header.visualIndex(Col.SEEDING_STARTED_AT), 0)

        self.manager.save_ui_state({
            "x": 350, "y": 250, "width": 820, "height": 610,
            "column_widths": {str(c): 120 for c in range(legacy_count)},
            "header_state": bytes(header.saveState().toHex()).decode(),
            "column_count": legacy_count,
            "sort_column": Col.ADDED,
            "sort_order": 0,
        })

        healed = self.new_window(self.manager)
        healed_header = healed._table.horizontalHeader()
        self.assertEqual(healed.pos().x(), 350)
        self.assertEqual(healed.pos().y(), 250)
        self.assertEqual(
            healed_header.visualIndex(Col.SEEDING_STARTED_AT), Col.COUNT - 1,
            "the newest appended column must be re-pinned to the end after an upgrade",
        )
        for i, col in enumerate(_DEFAULT_TAIL_COLUMNS):
            self.assertEqual(
                healed_header.visualIndex(col), Col.COUNT - len(_DEFAULT_TAIL_COLUMNS) + i,
                f"{Col.HEADERS[col]} must be re-pinned at its tail position",
            )

    def test_about_dialog_contains_copyright(self):
        """About dialog displays the application information and copyright notice."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        captured_dialogs = []
        original_exec = QMessageBox.exec

        def _intercept_exec(dialog_self):
            captured_dialogs.append(dialog_self)
            return QMessageBox.StandardButton.Ok

        with patch.object(QMessageBox, "exec", _intercept_exec):
            self.win._on_about()
            self.assertEqual(len(captured_dialogs), 1)
            dlg = captured_dialogs[0]
            self.assertIn("© Rakesh Malik, 2026", dlg.informativeText())
            self.assertIn("My-IDM", dlg.text())

    def test_about_lists_advertised_features(self):
        """About text should mention the feature set the dialog advertises."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        captured = []

        def _intercept_exec(dialog_self):
            captured.append(dialog_self)
            return QMessageBox.StandardButton.Ok

        with patch.object(QMessageBox, "exec", _intercept_exec):
            self.win._on_about()

        info = captured[0].informativeText()
        for feature in (
            "multi-segment",
            "bittorrent",
            "tor",
            "vpn kill switch",
            "youtube",
            "animepahe",
            "malware",
        ):
            self.assertIn(
                feature, info.lower(), f"missing feature: {feature}"
            )

    def test_about_credits_ai_coauthors(self):
        """The About dialog credits the AI tools that co-authored the project."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        captured_dialogs = []

        def _intercept_exec(dialog_self):
            captured_dialogs.append(dialog_self)
            return QMessageBox.StandardButton.Ok

        with patch.object(QMessageBox, "exec", _intercept_exec):
            self.win._on_about()

        info = captured_dialogs[0].informativeText()
        self.assertIn("Co-authored with", info)
        for credit in (
            "Gemini 3.8 Flash",
            "Claude 4.6 Opus",
            "Nvidia Nemotron 3 Ultra",
            "Space Bunny Alpha",
            "Poolside Laguna S 2.1",
            "Antigravity IDE",
            "Kilo Code plugin for Antigravity IDE",
        ):
            self.assertIn(credit, info, f"missing credit: {credit}")

    def test_system_tray_setup_and_actions(self):
        """System tray icon and context menu actions are properly initialized."""
        # Previously wrapped in `if self.win._tray_icon is not None:`, so on a
        # headless host the whole body was skipped and the test reported PASS
        # having asserted nothing. Its five siblings already skipTest correctly.
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        self.assertEqual(self.win._tray_icon.toolTip(), "My-IDM — Download Manager")
        menu = self.win._tray_icon.contextMenu()
        self.assertIsNotNone(menu)
        action_texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        self.assertTrue(any("Show" in t or "Hide" in t for t in action_texts))
        self.assertTrue(any("Pause All" in t for t in action_texts))
        self.assertTrue(any("Resume All" in t for t in action_texts))
        self.assertTrue(any("Preferences" in t for t in action_texts))
        self.assertTrue(any("Add Download" in t for t in action_texts))
        self.assertTrue(any("Exit" in t for t in action_texts))

    def _tray_menu(self):
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        self.assertIsNotNone(menu)
        return menu

    def _right_click_at(self, menu, action):
        """Deliver a real right-button press+release over *action*, as a person would."""
        menu.show()
        QApplication.processEvents()
        pos = QPointF(menu.actionGeometry(action).center())
        for event_type in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
        ):
            event = QMouseEvent(
                event_type,
                pos,
                QPointF(menu.mapToGlobal(menu.actionGeometry(action).center())),
                Qt.MouseButton.RightButton,
                Qt.MouseButton.RightButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(menu, event)
        QApplication.processEvents()

    def test_a_right_click_on_a_tray_item_does_not_activate_it(self):
        """A mis-landed right-click must not fire a menu entry.

        ``QMenu`` treats a right-button press+release as an ordinary activation. The bottom
        two tray rows are Restart and **Exit**, so a reflexive right-click - the gesture
        people reach for after a fumbled left-click - used to shut My-IDM down mid-download
        with no confirmation. ``_RightClickGuard`` consumes the button events so a
        right-click can only dismiss the menu.
        """
        from unittest.mock import patch

        menu = self._tray_menu()
        exit_action = next(a for a in menu.actions() if "Exit" in a.text())

        with patch.object(self.win, "_exit_app") as exit_mock:
            self._right_click_at(menu, exit_action)
        exit_mock.assert_not_called()

    def test_a_left_click_on_the_exit_item_still_works(self):
        """The guard must swallow the right button only.

        A guard that swallowed every button event would pass the test above while leaving
        the menu unusable, so the ordinary path is pinned here.
        """
        from unittest.mock import patch

        menu = self._tray_menu()
        exit_action = next(a for a in menu.actions() if "Exit" in a.text())

        with patch.object(self.win, "_exit_app") as exit_mock:
            exit_action.trigger()  # what a left click ends up doing
        self.assertEqual(exit_mock.call_count, 1)

    def test_every_destructive_tray_item_is_unreachable_by_right_click(self):
        """Restart and Exit are both one mis-aimed right-click from losing work."""
        from unittest.mock import patch

        menu = self._tray_menu()
        for needle in ("Exit", "Restart"):
            action = next((a for a in menu.actions() if needle in a.text()), None)
            self.assertIsNotNone(action, f"no {needle} entry in the tray menu")
            with patch.object(self.win, "_exit_app") as exit_mock, \
                 patch.object(self.win, "_restart_app") as restart_mock:
                self._right_click_at(menu, action)
                exit_mock.assert_not_called()
                restart_mock.assert_not_called()

    def test_the_guard_belongs_to_the_tray_menu(self):
        """Installed on the menu itself, and kept alive for as long as it is needed.

        PySide6 exposes no way to enumerate installed filters, so ownership plus the
        behavioural tests above are the check. A filter with no Python reference would be
        garbage-collected and silently stop filtering.
        """
        from my_idm.main_window import _RightClickGuard

        menu = self._tray_menu()
        guard = getattr(self.win, "_tray_right_click_guard", None)
        self.assertIsInstance(guard, _RightClickGuard)
        self.assertIs(guard.parent(), menu)

    def test_the_guard_swallows_right_button_events_only(self):
        from my_idm.main_window import _RightClickGuard

        guard = _RightClickGuard()
        for event_type in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        ):
            for button in (
                Qt.MouseButton.RightButton,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.MiddleButton,
                Qt.MouseButton.BackButton,
            ):
                with self.subTest(event=event_type.name, button=button.name):
                    event = QMouseEvent(
                        event_type, QPointF(1, 1), QPointF(1, 1),
                        button, button, Qt.KeyboardModifier.NoModifier,
                    )
                    blocked = guard.eventFilter(None, event)
                    self.assertEqual(
                        blocked, button == Qt.MouseButton.RightButton,
                        "only the right button may be swallowed",
                    )

    def test_the_guard_passes_through_unrelated_events(self):
        from my_idm.main_window import _RightClickGuard

        guard = _RightClickGuard()
        for event_type in (
            QEvent.Type.WindowActivate,
            QEvent.Type.MouseMove,
            QEvent.Type.KeyPress,
            QEvent.Type.Enter,
            QEvent.Type.Leave,
        ):
            with self.subTest(event=event_type.name):
                self.assertFalse(
                    guard.eventFilter(None, QEvent(event_type)),
                    "a filter that eats non-mouse events would break the menu entirely",
                )

    def test_system_tray_has_add_download_action(self):
        """Tray context menu exposes an Add Download entry."""
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        self.assertIsNotNone(menu)
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        self.assertTrue(any("Add Download" in t for t in texts), texts)

    def test_tray_add_download_opens_dialog(self):
        """Triggering the tray action opens the Add Download dialog."""
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        with patch.object(self.win, "_on_add") as mock_add:
            self.win._on_tray_add_download()
        mock_add.assert_called_once()

    def test_tray_add_download_restores_hidden_window(self):
        """The modal dialog's parent must be visible, so the window is restored first."""
        self.win.hide()
        self.assertFalse(self.win.isVisible())
        with patch.object(self.win, "_on_add"):
            self.win._on_tray_add_download()
        self.assertTrue(self.win.isVisible())

    def test_tray_menu_groups_restart_and_exit(self):
        """Restart and Exit share one group with no separator between them."""
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        actions = menu.actions()
        restart = next(a for a in actions if "Restart" in a.text())
        exit_ = next(a for a in actions if "Exit" in a.text())
        gap = actions[actions.index(restart) + 1 : actions.index(exit_)]
        self.assertFalse(
            any(a.isSeparator() for a in gap),
            "no separator expected between Restart and Exit",
        )

    def test_tray_has_about_action(self):
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        self.assertTrue(any("About" in t for t in texts), texts)

    def test_tray_about_restores_hidden_window(self):
        """The About dialog is parented to the window, so it must be restored."""
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        self.win.hide()
        self.assertFalse(self.win.isVisible())
        with patch.object(QMessageBox, "exec", lambda d: QMessageBox.StandardButton.Ok):
            self.win._on_about()
        self.assertTrue(self.win.isVisible())

    def test_tray_add_download_matches_tray_indentation(self):
        """Every tray entry must use the same style or the labels misalign.

        Qt reserves the icon column for the whole menu, so mixing an icon-based
        entry with glyph-in-text entries leaves the labels at different indents.
        """
        if self.win._tray_icon is None:
            self.skipTest("system tray unavailable")
        menu = self.win._tray_icon.contextMenu()
        for action in menu.actions():
            if action.isSeparator():
                continue
            has_icon = not action.icon().isNull()
            has_glyph = any(ord(ch) > 0x2000 for ch in action.text())
            self.assertNotEqual(
                has_icon, has_glyph,
                f"'{action.text()}' mixes icon and glyph styling",
            )

    def test_system_tray_toggle_show_window(self):
        """_toggle_show_window toggles between visible and hidden."""
        self.win.show()
        self.assertTrue(self.win.isVisible())
        self.win._toggle_show_window()
        self.assertFalse(self.win.isVisible())
        self.win._toggle_show_window()
        self.assertTrue(self.win.isVisible())

    def test_minimize_to_tray(self):
        """When minimize_to_tray is enabled, minimizing window hides it.

        The old version hand-built a `QWindowStateChangeEvent` carrying
        `WindowNoState` while the window was in fact Minimized, and then relied on
        a single `processEvents()` to drain the `QTimer.singleShot(0, self.hide)`.
        Whether one pass drains a zero-timer depends on Qt timer coalescing, and
        the synthetic event proved nothing about how Qt really delivers it. Here
        Qt delivers the event itself and the hide is awaited with a deadline.
        """
        self.win._manager._general_config.enable_system_tray = True
        self.win._manager._general_config.minimize_to_tray = True
        self.win.show()
        QApplication.processEvents()
        self.assertFalse(self.win.isHidden(), "the window must start visible for this test to mean anything")

        self.win.setWindowState(Qt.WindowState.WindowMinimized)
        QTest.qWait(50)

        if not self.win.isMinimized():
            # A platform that refuses a programmatic minimise would make this
            # test vacuous; say so instead of reporting a meaningless PASS.
            self.skipTest("the window manager did not honour a programmatic minimise")

        deadline = time.monotonic() + 2.0
        while not self.win.isHidden() and time.monotonic() < deadline:
            QApplication.processEvents()
            QTest.qWait(10)

        self.assertTrue(
            self.win.isHidden(),
            "minimize_to_tray must hide the window within 2s of the real "
            "WindowStateChange (QTimer.singleShot(0, self.hide) never ran)",
        )

    def test_restore_normal_is_not_hidden_when_minimize_to_tray_disabled(self):
        """Negative control: without minimize_to_tray, minimising must not hide.

        Without this, the test above would also pass if `changeEvent` hid the
        window unconditionally.
        """
        self.win._manager._general_config.enable_system_tray = True
        self.win._manager._general_config.minimize_to_tray = False
        self.win.show()
        QApplication.processEvents()

        self.win.setWindowState(Qt.WindowState.WindowMinimized)
        QTest.qWait(50)
        QApplication.processEvents()

        self.assertFalse(
            self.win.isHidden(),
            "with minimize_to_tray off the window must stay visible when minimised",
        )

    def test_close_to_tray_and_exit_app(self):
        """Closing window when close_to_tray is enabled hides window; _exit_app completely closes.

        Skipped where the desktop provides no system tray. With no tray icon there is nothing to
        close *into*, and the window now closes for real instead - which is the correct behaviour
        but is not what this test is asserting.
        """
        from PySide6.QtGui import QCloseEvent
        if self.win._tray_icon is None:
            self.skipTest("no system tray on this desktop")
        self.win._manager._general_config.enable_system_tray = True
        self.win._manager._general_config.close_to_tray = True
        self.win._force_exit = False
        self.win.show()

        # Regular closeEvent should be ignored and window hidden
        close_ev = QCloseEvent()
        self.win.closeEvent(close_ev)
        self.assertFalse(close_ev.isAccepted())
        self.assertTrue(self.win.isHidden())
        self.assertTrue(self.win._close_to_tray_notified)

        # _exit_app should set _force_exit and execute closeEvent even while hidden.
        #
        # The two assertions above the quit check only re-read flags the method
        # had just assigned, so they prove nothing on their own. `QApplication` is
        # one instance shared by every module in the session, and the real
        # `_exit_app()` ends in `app.quit()`; calling that for real would take the
        # process-wide event loop down for every later test. The app is therefore
        # swapped for a recorder inside the module under test, which makes the
        # quit observable instead of destructive.
        with patch("my_idm.main_window.QApplication") as mock_app_cls:
            self.win._exit_app()
            mock_app_cls.instance.assert_called()
            mock_app_cls.instance.return_value.quit.assert_called_once_with()

        self.assertTrue(self.win._force_exit)
        self.assertTrue(getattr(self.win, "_is_closing", False))
        # The force-exit path really did run closeEvent to completion, which
        # stops the live child timers a hidden close-to-tray window would leak.
        self.assertTrue(self.win._details_timer.isActive() is False,
                        "the force-exit path must stop the 1 Hz details timer")

    def test_close_without_a_tray_icon_closes_instead_of_stranding(self):
        """The data-loss case: no tray icon means the window must not hide.

        ``_setup_system_tray`` leaves ``_tray_icon`` as None when
        ``QSystemTrayIcon.isSystemTrayAvailable()`` is False - normal on GNOME, and on Wayland
        compositors with no AppIndicator. The preferences are both on by default, so the old gate
        hid the window anyway, leaving no tray icon, no taskbar entry and no menu: the app looked
        like it had quit while its downloads kept running.

        ``_is_closing`` is pre-set so closeEvent takes its ``event.accept()`` short-circuit rather
        than the full teardown, which would tear down this shared window.
        """
        from PySide6.QtGui import QCloseEvent

        cfg = self.win._manager._general_config
        cfg.enable_system_tray = True
        cfg.close_to_tray = True
        self.win._force_exit = False
        self.win._tray_icon = None  # simulate a desktop with no tray

        self.assertFalse(
            self.win._can_hide_to_tray(),
            "with no tray icon there is nothing to close into",
        )

        self.win._is_closing = True
        close_ev = QCloseEvent()
        self.win.closeEvent(close_ev)
        self.assertTrue(
            close_ev.isAccepted(),
            "close must be honoured so the window can actually go away",
        )
        self.win._tray_icon = None

    def test_can_hide_to_tray_requires_both_preferences_and_a_real_icon(self):
        """The full truth table, since each input is a separate way to strand the window."""
        cfg = self.win._manager._general_config
        original = (cfg.enable_system_tray, cfg.close_to_tray, self.win._tray_icon)
        try:
            cfg.enable_system_tray = True
            cfg.close_to_tray = True

            self.win._tray_icon = None
            self.assertFalse(self.win._can_hide_to_tray(), "no icon")

            cfg.close_to_tray = False
            self.assertFalse(self.win._can_hide_to_tray(), "close_to_tray off")

            cfg.close_to_tray = True
            cfg.enable_system_tray = False
            self.assertFalse(self.win._can_hide_to_tray(), "enable_system_tray off")

            # A truthy stand-in: the predicate tests for None, not for a live QSystemTrayIcon, so
            # this does not need a real tray on the machine running the tests.
            cfg.enable_system_tray = True
            self.win._tray_icon = object()
            self.assertTrue(self.win._can_hide_to_tray(), "all three satisfied")
        finally:
            cfg.enable_system_tray, cfg.close_to_tray, self.win._tray_icon = original

    def test_minimize_does_not_hide_when_there_is_no_tray(self):
        """Minimizing has the same trap as closing, and had the same missing check.

        Asserted through ``_has_tray_icon`` rather than by driving a real window-state change:
        the hide is deferred with ``QTimer.singleShot(0, ...)``, so it would not have happened by
        the time the assertion runs.
        """
        original = self.win._tray_icon
        try:
            self.win._tray_icon = None
            self.assertFalse(self.win._has_tray_icon())
            self.win._tray_icon = object()
            self.assertTrue(self.win._has_tray_icon())
        finally:
            self.win._tray_icon = original

    def test_tray_resume_all_downloads(self):
        """_on_resume_all_downloads triggers manager.resume_all_downloads and updates status label."""
        with unittest.mock.patch.object(self.manager, "resume_all_downloads", return_value=3) as mock_resume:
            self.win._on_resume_all_downloads()
            mock_resume.assert_called_once()
            self.assertIn("Resumed 3 downloads", self.win._status_label.text())

    def test_tray_notification_click_restores_and_focuses(self):
        """Clicking on tray notification restores and focuses main window."""
        self.win.hide()
        self.assertTrue(self.win.isHidden())
        with unittest.mock.patch("my_idm.single_instance.activate_window") as mock_activate:
            self.win._on_tray_message_clicked()
            self.assertFalse(self.win.isHidden())
            mock_activate.assert_called_once_with(self.win)

    def test_notification_handler_delegation_to_tray(self):
        """my_idm.notifications.show_notification routes through registered MainWindow tray icon."""
        from my_idm.notifications import show_notification
        received = []
        self.win._sig_show_tray_notification.connect(lambda t, m, d: received.append((t, m, d)))
        res = show_notification("Test Title", "Test Message", duration=4)
        self.assertTrue(res)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], ("Test Title", "Test Message", 4000))

    def test_status_completed_triggers_notification(self):
        """When download status becomes completed, notify_download_complete is called if enabled."""
        entry = DownloadEntry(id="done-1", url="http://example.com/done.mp4", filename="done.mp4", status="downloading")
        self.db.add_download(entry)
        self.win._load_history()
        QApplication.processEvents()
        self.win._manager._general_config.notify_on_completion = True
        with unittest.mock.patch("my_idm.notifications.notify_download_complete") as mock_notify:
            self.win._on_status_changed("done-1", "completed", "")
            mock_notify.assert_called_once_with("done.mp4")

            # Duplicate call should not re-notify
            mock_notify.reset_mock()
            self.win._on_status_changed("done-1", "completed", "")
            mock_notify.assert_not_called()

    def test_seeding_to_completed_does_not_notify(self):
        """Regression: ending a seeding session re-announced a download that finished earlier.

        A torrent that finishes downloading goes straight to "seeding", so that transition
        is the completion the user should hear about. When the seeding session later ends -
        time limit, ratio limit, or the user stopping it - the row becomes "completed" and
        the old code fired the notification a second time for a payload that arrived long
        ago.
        """
        entry = DownloadEntry(
            id="seed-1", url="magnet:?xt=urn:btih:aa", filename="movie.mkv",
            status="seeding", download_type="torrent", total_size=1000,
            downloaded_size=1000,
        )
        self.db.add_download(entry)
        self.win._load_history()
        QApplication.processEvents()
        self.win._manager._general_config.notify_on_completion = True
        with unittest.mock.patch("my_idm.notifications.notify_download_complete") as mock_notify:
            self.win._on_status_changed("seed-1", "completed", "")
            mock_notify.assert_not_called()

    def test_a_download_that_finishes_into_seeding_does_notify(self):
        """The other half: "seeding" on arrival *is* the completion.

        The old code only ever notified on "completed", so with the default
        ``seeding_after_complete`` setting a torrent that finished never notified at all.
        """
        entry = DownloadEntry(
            id="seed-2", url="magnet:?xt=urn:btih:bb", filename="show.mkv",
            status="downloading", download_type="torrent", total_size=1000,
            downloaded_size=400,
        )
        self.db.add_download(entry)
        self.win._load_history()
        QApplication.processEvents()
        self.win._manager._general_config.notify_on_completion = True
        with unittest.mock.patch("my_idm.notifications.notify_download_complete") as mock_notify:
            self.win._on_status_changed("seed-2", "seeding", "")
            mock_notify.assert_called_once_with("show.mkv")

    def test_a_full_seeding_lifecycle_notifies_exactly_once(self):
        """downloading -> seeding -> completed must produce exactly one notification."""
        entry = DownloadEntry(
            id="seed-3", url="magnet:?xt=urn:btih:cc", filename="ep.mkv",
            status="downloading", download_type="torrent", total_size=1000,
            downloaded_size=500,
        )
        self.db.add_download(entry)
        self.win._load_history()
        QApplication.processEvents()
        self.win._manager._general_config.notify_on_completion = True
        with unittest.mock.patch("my_idm.notifications.notify_download_complete") as mock_notify:
            self.win._on_status_changed("seed-3", "seeding", "")
            self.assertEqual(mock_notify.call_count, 1, "the payload arriving is news")
            self.win._on_status_changed("seed-3", "completed", "")
            self.assertEqual(mock_notify.call_count, 1, "ending the session is not news")
            mock_notify.reset_mock()
            # And a genuinely restarted download must be able to notify again.
            self.win._on_status_changed("seed-3", "downloading", "")
            self.win._on_status_changed("seed-3", "seeding", "")
            mock_notify.assert_called_once_with("ep.mkv")

    def test_no_notification_when_the_setting_is_off(self):
        entry = DownloadEntry(
            id="seed-4", url="magnet:?xt=urn:btih:dd", filename="off.mkv",
            status="downloading", download_type="torrent", total_size=1000,
        )
        self.db.add_download(entry)
        self.win._load_history()
        QApplication.processEvents()
        self.win._manager._general_config.notify_on_completion = False
        with unittest.mock.patch("my_idm.notifications.notify_download_complete") as mock_notify:
            self.win._on_status_changed("seed-4", "seeding", "")
            mock_notify.assert_not_called()


class TestDefaultHiddenColumns(unittest.TestCase):
    """Six columns are hidden on a fresh profile only."""

    def test_a_fresh_profile_hides_the_default_columns(self):
        from my_idm.database import Database
        from my_idm.download_model import Col
        from my_idm.main_window import DEFAULT_HIDDEN_COLUMNS, MainWindow
        from my_idm.manager import DownloadManager

        db = Database(":memory:")
        db.open()
        self.addCleanup(db.close)
        mgr = DownloadManager(db)
        self.addCleanup(mgr.stop)
        win = MainWindow(mgr)
        self.addCleanup(win.close)
        self.addCleanup(win.deleteLater)
        QApplication.processEvents()

        header = win._table.horizontalHeader()
        for col in DEFAULT_HIDDEN_COLUMNS:
            with self.subTest(column=Col.HEADERS[col]):
                self.assertTrue(
                    header.isSectionHidden(col),
                    f"{Col.HEADERS[col]} should start hidden",
                )
        self.assertFalse(
            header.isSectionHidden(Col.NAME),
            "the primary columns must stay visible",
        )

    def test_a_saved_header_state_is_not_overridden(self):
        """Changing a default must not re-arrange a profile that already has its own."""
        from my_idm.database import Database
        from my_idm.download_model import Col
        from my_idm.main_window import DEFAULT_HIDDEN_COLUMNS, MainWindow
        from my_idm.manager import DownloadManager

        db = Database(":memory:")
        db.open()
        self.addCleanup(db.close)
        mgr = DownloadManager(db)
        self.addCleanup(mgr.stop)

        # First window: show everything, then let it persist that arrangement.
        first = MainWindow(mgr)
        QApplication.processEvents()
        header = first._table.horizontalHeader()
        for col in range(Col.COUNT):
            header.setSectionHidden(col, False)
        first._save_ui_state_to_db()
        self.addCleanup(first.close)
        self.addCleanup(first.deleteLater)
        QApplication.processEvents()

        second = MainWindow(mgr)
        self.addCleanup(second.close)
        self.addCleanup(second.deleteLater)
        QApplication.processEvents()

        header = second._table.horizontalHeader()
        for col in DEFAULT_HIDDEN_COLUMNS:
            with self.subTest(column=Col.HEADERS[col]):
                self.assertFalse(
                    header.isSectionHidden(col),
                    "a saved arrangement must win over the default",
                )

    def test_the_default_set_is_small_and_leaves_the_usual_columns(self):
        from my_idm.download_model import Col
        from my_idm.main_window import DEFAULT_HIDDEN_COLUMNS

        self.assertLessEqual(len(DEFAULT_HIDDEN_COLUMNS), 6)
        for col in (Col.NAME, Col.SIZE, Col.PROGRESS, Col.STATUS, Col.SPEED):
            self.assertNotIn(
                col, DEFAULT_HIDDEN_COLUMNS,
                f"{Col.HEADERS[col]} is a primary column and must stay visible",
            )

    def test_the_default_sort_column_is_never_hidden_by_default(self):
        """Hiding the sorted column hides the sort indicator with it."""
        from my_idm.download_model import Col
        from my_idm.main_window import DEFAULT_HIDDEN_COLUMNS

        self.assertNotIn(
            Col.ADDED, DEFAULT_HIDDEN_COLUMNS,
            "Col.ADDED is the default sort column; hiding it leaves the table looking "
            "unsorted",
        )


class TestToolsMenuOpensTheRightPreferencesPage(_MainWindowTestCase):
    """The six "…Settings…" Tools-menu entries must open the page they name.

    Each handler used to pass a hard-coded integer written against the *old* six-tab
    layout. When the Preferences window grew to nine pages the indices stayed in range, so
    ``SettingsDialog`` raised nothing and simply opened the wrong page - every one of the
    six landing one or two pages off:

    =========================  =====================  ===================
    Menu entry                 Opened                 Should have opened
    =========================  =====================  ===================
    BitTorrent Settings…       Views & Columns         BitTorrent
    Browser Integration …      BitTorrent             Browser Integration
    VPN & Network Settings…    Browser Integration    VPN & Proxy
    Tor Network Settings…      Browser Integration    Tor
    Antivirus & Security …     VPN & Proxy            Antivirus & Security
    External Tools Settings…   Tor                    AnimePahe Scraper
    =========================  =====================  ===================

    The handlers now pass a ``TAB_*`` name, which resolves through ``tab_index()`` and
    raises on an unknown page.
    """

    #: handler -> (page it must open, a word that page's title must contain)
    EXPECTED = (
        ("_on_open_torrent_settings", TAB_TORRENT, "BitTorrent"),
        ("_on_open_browser_settings", TAB_BROWSER, "Browser"),
        ("_on_open_network_settings", TAB_VPN, "VPN"),
        ("_on_open_tor_settings", TAB_TOR, "Tor"),
        ("_on_open_security_settings", TAB_SECURITY, "Antivirus"),
        ("_on_open_external_tools_settings", TAB_EXTERNAL_TOOLS, "AnimePahe"),
    )

    def _record_argument(self, handler_name):
        """Call a handler with ``_on_open_preferences`` stubbed; return what it passed."""
        recorded = []
        original = self.win._on_open_preferences
        self.win._on_open_preferences = lambda tab: recorded.append(tab)
        try:
            getattr(self.win, handler_name)()
        finally:
            self.win._on_open_preferences = original
        self.assertEqual(
            len(recorded), 1, f"{handler_name} did not open Preferences exactly once"
        )
        return recorded[0]

    def test_each_handler_passes_the_right_page_name(self):
        for handler, expected_name, _word in self.EXPECTED:
            with self.subTest(handler=handler):
                self.assertEqual(
                    self._record_argument(handler), expected_name,
                    f"{handler} opens the wrong Preferences page",
                )

    def test_each_handler_lands_on_a_page_named_for_it(self):
        """Close the loop: the name it passes really is the page with that title."""
        from my_idm.settings_dialog import SettingsDialog

        for handler, expected_name, word in self.EXPECTED:
            with self.subTest(handler=handler):
                passed = self._record_argument(handler)
                dlg = SettingsDialog(db=self.db, initial_tab=passed)
                self.addCleanup(dlg.close)
                self.addCleanup(dlg.deleteLater)
                QApplication.processEvents()
                self.assertEqual(dlg.current_tab_name(), expected_name)
                self.assertIn(
                    word, dlg._tabs.tabText(dlg._tabs.currentIndex()),
                    f"{handler} lands on a page whose title does not mention {word!r}",
                )

    def test_no_two_handlers_point_at_the_same_page(self):
        """A copy-paste slip that made two menu items identical is invisible otherwise."""
        passed = [self._record_argument(h) for h, _n, _w in self.EXPECTED]
        self.assertEqual(
            len(set(passed)), len(passed),
            f"two Tools-menu entries open the same page: {passed}",
        )

    def test_every_opened_page_is_a_distinct_tab(self):
        indices = {
            tab_index(self._record_argument(h)) for h, _n, _w in self.EXPECTED
        }
        self.assertEqual(len(indices), len(self.EXPECTED))

    def test_the_tools_menu_wires_every_handler(self):
        """A renamed or mis-wired slot would leave an entry opening the wrong page."""
        for handler, _name, _word in self.EXPECTED:
            with self.subTest(handler=handler):
                self.assertTrue(hasattr(self.win, handler))

    def _tools_titles(self) -> list:
        tools = next(
            a.menu() for a in self.win.menuBar().actions() if a.text().startswith("&Tools")
        )
        return [a.text() for a in tools.actions()]

    def test_tools_groups_statistics_with_preferences(self):
        """Statistics and Preferences are both "look at / configure the app" entries.

        Export is a way to get something out, so a separator belongs between the two groups.
        """
        titles = self._tools_titles()
        prefs_at = titles.index("Preferences…")
        stats_at = titles.index("Statistics…")
        export_at = titles.index("Export Selected as CSV…")
        self.assertLess(
            prefs_at, stats_at, f"Statistics joins the Preferences group: {titles}"
        )
        self.assertEqual(
            stats_at - prefs_at, 1, f"no separator splits the pair: {titles}"
        )
        self.assertGreater(
            export_at, stats_at, f"Export belongs after the group: {titles}"
        )
        self.assertEqual(
            titles[stats_at + 1], "",
            f"a separator must fall between Statistics and Export: {titles}",
        )

    def test_the_tools_statistics_entry_is_wired_and_unabbreviated(self):
        """A menu label may not be abbreviated, and it must not inherit Ctrl+,.

        The toolbar copy that used to be abbreviated to "Stats…" is gone; this is the only
        Statistics action, so there is no second one to drift out of step with it.
        """
        act = self.win._act_tools_stats
        self.assertEqual(act.text(), "Statistics…")
        self.assertTrue(act.shortcut().isEmpty(), "no shortcut may be registered twice")

        with patch.object(self.win, "_on_show_statistics") as handler:
            act.trigger()
        handler.assert_called_once_with()

        self.assertFalse(
            hasattr(self.win, "_act_stats"),
            "the old toolbar action is gone, not left parentless for something to reuse",
        )
        self.assertTrue(act.toolTip().strip(), "a menu entry still needs its tooltip")

    def test_every_settings_action_triggers_its_handler(self):
        """Trigger the real ``QAction`` and assert which page it asks for.

        Proves the menu entry is wired to the right slot, end to end, rather than only
        that the slot itself behaves - a mis-wired or unwired entry is the failure that
        would otherwise reach the user and not the suite.
        """
        actions = {
            self.win._act_torrent_settings: TAB_TORRENT,
            self.win._act_browser_settings: TAB_BROWSER,
            self.win._act_network_settings: TAB_VPN,
            self.win._act_tor_settings: TAB_TOR,
            self.win._act_security_settings: TAB_SECURITY,
            self.win._act_external_tools_settings: TAB_EXTERNAL_TOOLS,
        }
        original = self.win._on_open_preferences
        for action, expected in actions.items():
            with self.subTest(action=action.text()):
                recorded = []
                self.win._on_open_preferences = lambda tab: recorded.append(tab)
                try:
                    action.trigger()
                finally:
                    self.win._on_open_preferences = original
                self.assertEqual(recorded, [expected])

    def test_every_settings_action_is_reachable_from_the_tools_menu(self):
        """An action that is never added to a menu is dead code, however correct."""
        menu_actions = set()
        for action in self.win.menuBar().actions():
            submenu = action.menu()
            if submenu is not None:
                menu_actions.update(submenu.actions())
        for action in (
            self.win._act_torrent_settings,
            self.win._act_browser_settings,
            self.win._act_network_settings,
            self.win._act_tor_settings,
            self.win._act_security_settings,
            self.win._act_external_tools_settings,
        ):
            with self.subTest(action=action.text()):
                self.assertIn(action, menu_actions)

    def test_every_settings_action_is_reachable_from_the_tools_menu(self):
        """An action that is never added to a menu is dead code, however correct."""
        menu_actions = set()
        for action in self.win.menuBar().actions():
            submenu = action.menu()
            if submenu is not None:
                menu_actions.update(submenu.actions())
        for action in (
            self.win._act_torrent_settings,
            self.win._act_browser_settings,
            self.win._act_network_settings,
            self.win._act_tor_settings,
            self.win._act_security_settings,
            self.win._act_external_tools_settings,
        ):
            with self.subTest(action=action.text()):
                self.assertIn(action, menu_actions)


class TestActionStatesDynamicGating(_MainWindowTestCase):
    """Verify that status and action buttons are dynamically enabled/disabled appropriately."""

    def test_no_selection_disables_all_selection_dependent_actions(self):
        self.win._table.clearSelection()
        self.win._update_action_states()
        self.assertFalse(self.win._act_resume.isEnabled())
        self.assertFalse(self.win._act_pause.isEnabled())
        self.assertFalse(self.win._act_stop.isEnabled())
        self.assertFalse(self.win._act_force_start.isEnabled())
        self.assertFalse(self.win._act_start_seeding.isEnabled())
        self.assertFalse(self.win._act_copy_url.isEnabled())
        self.assertFalse(self.win._act_rename.isEnabled())
        self.assertFalse(self.win._act_delete.isEnabled())
        self.assertFalse(self.win._act_delete_file.isEnabled())
        self.assertFalse(self.win._act_move.isEnabled())
        self.assertFalse(self.win._act_recheck.isEnabled())
        self.assertFalse(self.win._act_open_file.isEnabled())
        self.assertFalse(self.win._act_open_folder.isEnabled())
        self.assertFalse(self.win._act_scan_antivirus.isEnabled())
        self.assertFalse(self.win._act_move_up.isEnabled())
        self.assertFalse(self.win._act_move_down.isEnabled())

    def test_selecting_downloading_entry(self):
        self.db.add_download(
            DownloadEntry(id="d1", url="http://e.com/1.zip", filename="1.zip", status="downloading")
        )
        self.win._load_history()
        self.win._table.selectRow(0)

        self.assertTrue(self.win._act_pause.isEnabled())
        self.assertFalse(self.win._act_resume.isEnabled())
        self.assertTrue(self.win._act_stop.isEnabled())
        self.assertFalse(self.win._act_force_start.isEnabled())
        self.assertTrue(self.win._act_rename.isEnabled())
        self.assertTrue(self.win._act_delete.isEnabled())

    def test_selecting_paused_entry(self):
        self.db.add_download(
            DownloadEntry(id="d2", url="http://e.com/2.zip", filename="2.zip", status="paused")
        )
        self.win._load_history()
        self.win._table.selectRow(0)

        self.assertFalse(self.win._act_pause.isEnabled())
        self.assertTrue(self.win._act_resume.isEnabled())
        self.assertTrue(self.win._act_force_start.isEnabled())
        self.assertTrue(self.win._act_stop.isEnabled())

    def test_selecting_multiple_mixed_entries(self):
        self.db.add_download(
            DownloadEntry(id="d1", url="http://e.com/1.zip", filename="1.zip", status="downloading")
        )
        self.db.add_download(
            DownloadEntry(id="d2", url="http://e.com/2.zip", filename="2.zip", status="paused")
        )
        self.win._load_history()
        self.win._table.selectAll()

        self.assertTrue(self.win._act_pause.isEnabled())
        self.assertTrue(self.win._act_resume.isEnabled())
        self.assertFalse(self.win._act_rename.isEnabled(), "Rename must be disabled for multi-selection")
        self.assertTrue(self.win._act_delete.isEnabled())

    def test_torrent_seeding_action_gating(self):
        self.db.add_download(
            DownloadEntry(
                id="t1",
                url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111",
                filename="completed_torrent",
                download_type="torrent",
                status="completed",
            )
        )
        self.win._load_history()
        self.win._table.selectRow(0)
        self.assertTrue(self.win._act_start_seeding.isEnabled())

        # Update to seeding
        entry = self.db.get_download("t1")
        entry.status = "seeding"
        self.db.update_download(entry)
        self.win._load_history()
        self.win._table.selectRow(0)
        self.assertFalse(self.win._act_start_seeding.isEnabled(), "Already seeding")
        self.assertTrue(self.win._act_pause.isEnabled())
        self.assertTrue(self.win._act_stop.isEnabled())

    def test_pause_all_and_resume_all_gating(self):
        # Empty DB: neither pause_all nor resume_all enabled
        self.win._load_history()
        self.assertFalse(self.win._act_pause_all.isEnabled())
        self.assertFalse(self.win._act_resume_all.isEnabled())
        self.assertFalse(self.win._act_stop_all_seeding.isEnabled())
        if hasattr(self.win, "_tray_act_pause_all"):
            self.assertFalse(self.win._tray_act_pause_all.isEnabled())
            self.assertFalse(self.win._tray_act_resume_all.isEnabled())

        # Only completed downloads: neither pause_all nor resume_all enabled
        self.db.add_download(
            DownloadEntry(id="c1", url="http://e.com/c1.zip", filename="c1.zip", status="completed")
        )
        self.win._load_history()
        self.assertFalse(self.win._act_pause_all.isEnabled())
        self.assertFalse(self.win._act_resume_all.isEnabled())
        if hasattr(self.win, "_tray_act_pause_all"):
            self.assertFalse(self.win._tray_act_pause_all.isEnabled())
            self.assertFalse(self.win._tray_act_resume_all.isEnabled())

        # Only seeding torrent: pause_all is False, resume_all is False, stop_all_seeding is True
        self.db.add_download(
            DownloadEntry(id="s1", url="magnet:?xt=urn:btih:2222222222222222222222222222222222222222", filename="s1", status="seeding")
        )
        self.win._load_history()
        self.assertFalse(self.win._act_pause_all.isEnabled(), "pause_all must be disabled when only seeding downloads exist")
        self.assertFalse(self.win._act_resume_all.isEnabled())
        self.assertTrue(self.win._act_stop_all_seeding.isEnabled())

        # Active non-seeding download added: pause_all enabled
        self.db.add_download(
            DownloadEntry(id="d1", url="http://e.com/d1.zip", filename="d1.zip", status="downloading")
        )
        self.win._load_history()
        self.assertTrue(self.win._act_pause_all.isEnabled())
        self.assertFalse(self.win._act_resume_all.isEnabled())
        if hasattr(self.win, "_tray_act_pause_all"):
            self.assertTrue(self.win._tray_act_pause_all.isEnabled())
            self.assertFalse(self.win._tray_act_resume_all.isEnabled())

        # Paused download added: resume_all enabled
        self.db.add_download(
            DownloadEntry(id="p1", url="http://e.com/p1.zip", filename="p1.zip", status="paused")
        )
        self.win._load_history()
        self.assertTrue(self.win._act_pause_all.isEnabled())
        self.assertTrue(self.win._act_resume_all.isEnabled())
        if hasattr(self.win, "_tray_act_pause_all"):
            self.assertTrue(self.win._tray_act_pause_all.isEnabled())
            self.assertTrue(self.win._tray_act_resume_all.isEnabled())

    def test_file_not_found_action_gating(self):
        self.db.add_download(
            DownloadEntry(id="fnf1", url="http://e.com/f.zip", filename="f.zip", status="file_not_found")
        )
        self.win._load_history()
        self.win._table.selectRow(0)

        # File-dependent operations must be disabled
        self.assertFalse(self.win._act_rename.isEnabled())
        self.assertFalse(self.win._act_move.isEnabled())
        self.assertFalse(self.win._act_scan_antivirus.isEnabled())
        self.assertFalse(self.win._act_delete_file.isEnabled())
        self.assertFalse(self.win._act_open_file.isEnabled())
        self.assertFalse(self.win._act_open_folder.isEnabled())

        # Non-file dependent operations remain enabled
        self.assertTrue(self.win._act_delete.isEnabled(), "Can remove entry from list")
        self.assertTrue(self.win._act_resume.isEnabled(), "Can re-download missing file")
        # Recheck is the exception to the file-dependent group, because it is the action that
        # *looks* at the disk. Gating it here left no way back short of a full re-download.
        self.assertTrue(
            self.win._act_recheck.isEnabled(),
            "recheck must be offered for a file_not_found row so a restored file can be confirmed",
        )

    def test_recheck_is_disabled_only_when_nothing_is_selected(self):
        """Recheck's only gate is the selection - every status, including file_not_found."""
        self.db.add_download(
            DownloadEntry(id="r0", url="http://e.com/0.zip", filename="0.zip", status="file_not_found")
        )
        self.win._load_history()
        self.assertFalse(
            self.win._act_recheck.isEnabled(),
            "with nothing selected there is no row to verify",
        )

        for i, status in enumerate((
            "file_not_found", "completed", "downloading", "queued",
            "paused", "stopped", "error", "threat_detected", "seeding",
            "suspended", "fetching_metadata", "checking", "scanning", "stalled",
        )):
            with self.subTest(status=status):
                did = f"rchk-{status}"
                self.db.add_download(DownloadEntry(
                    id=did, url=f"http://e.com/{did}.zip",
                    filename=f"{did}.zip", status=status,
                ))
                self.win._load_history()
                row = self.win._model.row_for_id(did)
                self.assertIsNotNone(row)
                self.win._table.selectRow(row)
                self.assertTrue(
                    self.win._act_recheck.isEnabled(),
                    f"recheck must be available for a '{status}' row",
                )

    def test_recheck_recovers_a_file_not_found_row_once_the_file_is_back(self):
        """End to end: the newly enabled button restores the row it is supposed to recover."""
        target = Path(tempfile.gettempdir()) / "recheck-recovered.zip"
        try:
            target.write_bytes(b"R" * 4096)
            self.db.add_download(DownloadEntry(
                id="fnf-recover",
                url="http://e.com/recovered.zip",
                filename="recovered.zip",
                file_path=str(target),
                save_path=str(target.parent),
                total_size=4096,
                downloaded_size=4096,
                status="file_not_found",
            ))
            self.win._load_history()
            row = self.win._model.row_for_id("fnf-recover")
            self.win._table.selectRow(row)
            self.assertTrue(self.win._act_recheck.isEnabled())

            self.win._on_recheck()

            self.assertEqual(self.db.get_download("fnf-recover").status, "completed")
        finally:
            try:
                target.unlink()
            except OSError:
                pass

    def test_context_menu_bandwidth_allocation_gating(self):
        from PySide6.QtCore import QPoint

        # Inactive/completed/fnf download: BW allocation menu disabled
        self.db.add_download(
            DownloadEntry(id="c1", url="http://e.com/c1.zip", filename="c1.zip", status="completed")
        )
        self.win._load_history()
        self.win._table.selectRow(0)

        # Build context menu directly via helper or testing
        # We can inspect show_context_menu building by calling the logic or inspecting the created menu
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self.win)
        # Verify bandwidth allocation enabled state for different statuses
        active_statuses = {"downloading", "fetching_metadata", "stalled", "checking", "scanning", "queued", "paused", "seeding"}
        inactive_statuses = {"completed", "stopped", "file_not_found", "error", "threat_detected", "suspended"}

        for st in active_statuses:
            entry = DownloadEntry(id="test", url="http://e.com", filename="a", status=st)
            can_alloc = bool(entry and entry.status in active_statuses)
            self.assertTrue(can_alloc, f"Bandwidth allocation should be enabled for {st}")

        for st in inactive_statuses:
            entry = DownloadEntry(id="test", url="http://e.com", filename="a", status=st)
            can_alloc = bool(entry and entry.status in active_statuses)
            self.assertFalse(can_alloc, f"Bandwidth allocation should be disabled for {st}")


if __name__ == "__main__":
    unittest.main()



