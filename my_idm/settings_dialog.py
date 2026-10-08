"""Preferences and settings dialog for My-IDM."""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional, Any

from PySide6.QtCore import Qt, QSize, QUrl, QSettings, QObject, Signal, QTime, QRect
from PySide6.QtGui import QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence, QColor, QPixmap, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QLayout,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QAbstractItemView,
    QHeaderView,
    QVBoxLayout,
    QWidget,
    QStyle,
)

import asyncio
import aiohttp
import humanize
from my_idm.config import (
    GeneralConfig,
    TorConfig,
    TorrentConfig,
    ExternalToolsConfig,
    BrowserIntegrationConfig,
    BandwidthLimitConfig,
    SchedulerConfig,
    clamp_ytdlp_playlist_limit,
    is_tor_reachable,
    DEFAULT_DOWNLOADS_DIR,
    MAX_SEGMENT_START_DELAY_MS,
    normalize_extension_list,
)
from my_idm.database import Database, APP_DIR, DEFAULT_QUEUE_COLOR, normalize_queue_color
from my_idm.download_model import Col, _format_speed
from my_idm.dialogs import AddQueueDialog
from my_idm.external_tools import (
    launch_animepahe_cli,
    launch_animepahe_gui,
    open_file_in_default_app,
    show_in_folder,
)
from my_idm.utils import normalize_path
from my_idm.tor_service import find_tor_executable
from my_idm.network import (
    NetworkConfig,
    NetworkInterfaceInfo,
    get_available_interfaces,
)
from my_idm.security import (
    KNOWN_THREAT_CATEGORIES,
    SecurityConfig,
    find_windows_defender_path,
    scan_file,
)

from my_idm import fonts

log = logging.getLogger(__name__)


def _create_action_icon(emoji: str, size: int = 24) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = fonts.emoji_font(12)
    font.setPixelSize(int(size * 0.75))
    p.setFont(font)
    p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, emoji)
    p.end()
    return QIcon(pix)

#: Symbolic indices for the Preferences pages, in the order ``_setup_ui`` adds them.
#:
#: Every caller that wants to open a *specific* page - the six "…Settings…" Tools-menu
#: entries in `MainWindow` - must pass one of these names, never a bare integer. A bare
#: integer silently rots the moment a tab is inserted or split: the 6 -> 9 tab split in
#: commit 9202ec8 left six menu items pointing at valid but wrong pages, and
#: ``SettingsDialog.__init__`` clamps with ``0 <= initial_tab < count()`` so nothing raised
#: and the failure was invisible. A name that does not exist is a loud ``KeyError``.
TAB_GENERAL = "general"
TAB_APP = "app"
TAB_CLIPBOARD = "clipboard"
TAB_VIEWS = "views"
TAB_TORRENT = "torrent"
TAB_BROWSER = "browser"
TAB_VPN = "vpn"
TAB_TOR = "tor"
TAB_SECURITY = "security"
TAB_EXTERNAL_TOOLS = "external_tools"
TAB_YOUTUBE = "youtube"
TAB_QUEUES = "queues"
TAB_BANDWIDTH = "bandwidth"
TAB_SCHEDULER = "scheduler"

#: Name -> insertion index. Order here *is* the tab order; ``SettingsDialog._setup_ui``
#: adds the pages in exactly this sequence and the tests pin the two against each other.
TAB_ORDER: tuple[str, ...] = (
    TAB_GENERAL,
    TAB_APP,
    TAB_CLIPBOARD,
    TAB_VIEWS,
    TAB_TORRENT,
    TAB_BROWSER,
    TAB_VPN,
    TAB_TOR,
    TAB_SECURITY,
    TAB_EXTERNAL_TOOLS,
    TAB_YOUTUBE,
    TAB_QUEUES,
    TAB_BANDWIDTH,
    TAB_SCHEDULER,
)

#: Titles as shown in the sidebar, keyed by the same names. Used by the tests and by
#: ``MainWindow`` when it reports which page a menu item will open.
TAB_TITLES: dict[str, str] = {
    TAB_GENERAL: "📁 Downloads & Retries",
    TAB_APP: "🖥️ Application & Tray",
    TAB_CLIPBOARD: "📋 Clipboard Capture",
    TAB_VIEWS: "👁️ Views & Columns",
    TAB_TORRENT: "🧲 BitTorrent",
    TAB_BROWSER: "🌐 Browser Integration",
    TAB_VPN: "🛡️ VPN & Proxy",
    TAB_TOR: "🧅 Tor",
    TAB_SECURITY: "🛡️ Antivirus & Security",
    TAB_EXTERNAL_TOOLS: "🌐 AnimePahe Scraper",
    TAB_YOUTUBE: "▶️ YouTube (yt-dlp)",
    TAB_QUEUES: "⚙️ Queues",
    TAB_BANDWIDTH: "📊 Bandwidth Limit",
    TAB_SCHEDULER: "⏱️ Scheduler",
}


from my_idm.styles import (
    DEFAULT_THEME,
    THEME_LABELS,
    THEME_NAMES,
    apply_theme,
    normalize_theme,
    themed_widget,
)


def tab_index(name: str) -> int:
    """Index of the Preferences page called *name*.

    Raises ``KeyError`` for an unknown page rather than returning a plausible default: a
    menu item pointed at the wrong page is a bug that is invisible to the user until they
    click it, so it must not be something that compiles.
    """
    return TAB_ORDER.index(name)


class _WholeRowListWidget(QListWidget):
    """A ``QListWidget`` that owns its own height, so it always ends on a row boundary.

    The downloads-table column list was laid out with a stretch, so its height was whatever
    the dialog had left over. That lands on an arbitrary pixel value and the last visible row
    ends up **bisected** by the bottom of the frame - a half-height checkbox and a name cut
    through the middle.

    Snapping the viewport afterwards was not enough, because it depends on
    ``sizeHintForRow(0)`` agreeing with the height Qt actually paints. Two earlier attempts
    were wrong in different ways: one clamped the viewport and so could never grow back, and
    one shrank the list to make room on the page, which hid half the columns instead. Both
    were pixel-snapping a height the layout still owned.

    So the widget fixes its own height instead: ``Fixed`` vertical policy plus an explicit
    multiple of the measured row height. The layout can no longer hand it a height that
    bisects a row, whatever the font, DPI or window size. The remainder goes to the group box
    and the page, which is what the surrounding scroll area is for.
    """

    #: Rows to show at the dialog's minimum height: the whole column set, which fits at the font
    #: size this ships with. The list scrolls if a user's font makes them taller.
    #:
    #: Derived from ``Col.COUNT`` rather than written as a number. It used to be a literal 17,
    #: and adding a column left the list one row short - which the existing test caught, but
    #: only because it happened to compare against the column count.
    DEFAULT_VISIBLE_ROWS = Col.COUNT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._visible_rows = self.DEFAULT_VISIBLE_ROWS
        self._clamp_to_whole_rows()

    def setVisibleRows(self, rows: int) -> None:
        """Show *rows* whole rows, dropping any partial one."""
        self._visible_rows = max(1, int(rows))
        self._clamp_to_whole_rows()

    def _clamp_to_whole_rows(self) -> None:
        row = self.sizeHintForRow(0)
        if not row or row <= 0:
            # No items yet, so the real row height is unknown. Guessing one is worse than
            # waiting: a fallback derived from the font runs ~28px against a real ~17px, and
            # seventeen of those inflates the page by hundreds of pixels. Leave the height to
            # the layout until the items exist, then `relayout_rows()` fixes it.
            self.setMinimumHeight(120)
            return
        self.setMinimumHeight(0)
        # The widget is taller than its viewport by the frame plus the viewport margins, so
        # sizing it to `row * n` cuts the last row: 17 rows of 17px is 289px of content in a
        # 281px viewport. Add the chrome back, otherwise the fix reproduces the defect it
        # was written for.
        margins = self.viewportMargins()
        chrome = 2 * self.frameWidth() + margins.top() + margins.bottom()
        self.setFixedHeight(row * self._visible_rows + chrome)

    def relayout_rows(self) -> None:
        """Re-fix the height from the measured row height. Call after populating items."""
        self._clamp_to_whole_rows()

    def showEvent(self, event):
        super().showEvent(event)
        # Row height is only knowable once there are items and a resolved font.
        self._clamp_to_whole_rows()

    def rows_that_fit(self) -> int:
        """How many whole rows are currently visible."""
        row = self.sizeHintForRow(0)
        # `sizeHintForRow` returns -1 when there are no items, and -1 is truthy, so the
        # guard has to be `<= 0` rather than a bare truthiness check.
        if row is None or row <= 0:
            return 0
        return self.viewport().height() // row

    def has_partially_visible_row(self) -> bool:
        """True when the viewport cuts a row instead of ending cleanly between two."""
        row = self.sizeHintForRow(0)
        if not row or row <= 0 or self.count() == 0:
            return False
        viewport = self.viewport().rect()
        for index in range(self.count()):
            rect = self.visualItemRect(self.item(index))
            if viewport.contains(rect):
                continue
            if viewport.intersects(rect):
                return True
        return False


class _PrefsProbeEmitter(QObject):
    """Carries probe results back to the GUI thread.

    Deliberately a bare ``QObject`` and not a ``QThread``. A ``QThread`` that is destroyed while
    still running aborts the process, and the dialog owns the thread, so any path that drops the
    dialog without closing it - a test that forgets, a GC, an early return - is a hard crash. That
    failure is intermittent by nature, which is the worst kind to have.

    A plain ``threading.Thread(daemon=True)`` has no such semantics: it completes on its own, and
    if the receiver is gone the queued emit is simply delivered to nobody.

    Not parented to the dialog either. A child QObject is destroyed with its parent, and the worker
    may still be holding this one when that happens; the resulting emit raises. Unparented, the
    emitter outlives the dialog until the worker's last reference goes away, and `run()` guards the
    emit as well.
    """

    finished = Signal(object)


class _PrefsProbeWorker(threading.Thread):
    """Runs the Preferences dialog's blocking system probes off the GUI thread.

    Three things the dialog wants to display require leaving the GUI thread to be worth doing at
    all: importing ``yt_dlp`` (~300 ms the first time), enumerating network adapters via psutil
    (~25 ms), and walking the filesystem for a Tor executable (~14 ms). Together they dominated the
    dialog's open time, and every one is a *display* concern - nothing depends on them to decide
    what to save.

    So they run once, at construction, and the results come back through `emitter.finished`. Nothing
    here touches a widget; only `_apply_probe_results` does, and only on the GUI thread, which is
    the rule Qt actually enforces.

    Each probe is isolated so one failure cannot stop the others from being reported.
    """

    def __init__(self, emitter: _PrefsProbeEmitter, tor_hint: str = ""):
        super().__init__(daemon=True, name="prefs-probe")
        self._emitter = emitter
        self._tor_hint = tor_hint

    def run(self):
        # Imported inside run() so the module-level cost is paid here rather than at dialog import,
        # and so a failure in any one of them cannot stop the others.
        results = {}
        try:
            from my_idm.network import get_available_interfaces

            results["interfaces"] = get_available_interfaces()
        except Exception as exc:
            log.debug("Interface probe failed: %s", exc)
            results["interfaces"] = []

        try:
            from my_idm.config import ExternalToolsConfig
            from my_idm import youtube_tool as ytt

            cfg = ExternalToolsConfig()
            ytdlp_path = cfg.get_effective_ytdlp_path()
            results["ytdlp_path"] = ytdlp_path
            results["ytdlp_version"] = (
                ytt.get_ytdlp_version(cfg) if ytdlp_path else ""
            )
            results["ffmpeg_path"] = cfg.get_effective_ffmpeg_path()
        except Exception as exc:
            log.debug("yt-dlp probe failed: %s", exc)
            results["ytdlp_path"] = ""
            results["ytdlp_version"] = ""
            results["ffmpeg_path"] = ""

        try:
            from my_idm.tor_service import find_tor_executable

            results["tor"] = self._tor_hint or find_tor_executable() or ""
        except Exception as exc:
            log.debug("Tor probe failed: %s", exc)
            results["tor"] = ""

        try:
            self._emitter.finished.emit(results)
        except RuntimeError:
            # The dialog (and with it the emitter, which is its child) was destroyed while this
            # probe was in flight. There is nobody left to tell, which is the correct outcome for
            # a dialog that has closed - but emitting into a deleted QObject raises, and an
            # exception escaping a daemon thread surfaces as a test-suite warning.
            pass


class SettingsDialog(QDialog):
    """Preferences / Settings dialog for general downloads, torrent, network, Tor, security, and browser."""

    def __init__(
        self,
        general_config: Optional[GeneralConfig] = None,
        torrent_config: Optional[TorrentConfig] = None,
        network_config: Optional[NetworkConfig] = None,
        security_config: Optional[SecurityConfig] = None,
        tor_config: Optional[TorConfig] = None,
        external_tools_config: Optional[ExternalToolsConfig] = None,
        browser_config: Optional[BrowserIntegrationConfig] = None,
        scheduler_config: Optional[SchedulerConfig] = None,
        db: Optional[Database] = None,
        parent=None,
        initial_tab=0,
        manager=None,
    ):
        """*initial_tab* is either an index (legacy) or a ``TAB_*`` name (preferred).

        A name is resolved through ``tab_index()``, which raises on an unknown page. That
        is deliberate: an out-of-range integer is clamped away and silently shows the wrong
        page, which is exactly the bug this parameter had.
        """
        super().__init__(parent)
        self.setWindowTitle("Preferences & Settings")
        self.setMinimumWidth(740)
        # 668, not 560: every page sits in a QScrollArea, and the Views page - the tallest -
        # overflowed it at 560, so the dialog scrolled at its own minimum size. Scrolling a
        # preferences page is legitimate; scrolling it because the minimum is too small to
        # hold the content is not, and the symptom is the page looking cropped. 668 is
        # measured, not guessed: see `test_the_views_page_never_needs_to_scroll`.
        self.setMinimumHeight(668)
        self.setModal(True)
        self._db = db
        self._manager = manager if manager is not None else getattr(parent, "_manager", None)
        # Set by _on_download_animepahe_url when the scraper was actually started
        # from within the dialog, so the main window can switch the bottom panel
        # to the live console instead of leaving the user hunting for progress.
        self.animepahe_download_started = False

        from my_idm.resources import get_app_icon
        self.setWindowIcon(get_app_icon())

        self._general_cfg = (
            GeneralConfig.from_dict(general_config.to_dict())
            if general_config
            else GeneralConfig.load()
        )
        self._torrent_cfg = (
            TorrentConfig.from_dict(torrent_config.to_dict())
            if torrent_config
            else TorrentConfig.load()
        )
        self._network_cfg = (
            NetworkConfig.from_dict(network_config.to_dict())
            if network_config
            else NetworkConfig.load()
        )
        self._security_cfg = (
            SecurityConfig.from_dict(security_config.to_dict())
            if security_config
            else SecurityConfig.load()
        )
        self._tor_cfg = (
            TorConfig.from_dict(tor_config.to_dict())
            if tor_config
            else TorConfig.load()
        )
        self._external_tools_cfg = (
            ExternalToolsConfig.from_dict(external_tools_config.to_dict())
            if external_tools_config
            else ExternalToolsConfig.load()
        )
        self._browser_cfg = (
            BrowserIntegrationConfig.from_dict(browser_config.to_dict())
            if browser_config
            else BrowserIntegrationConfig.load()
        )
        self._scheduler_cfg = (
            SchedulerConfig.from_dict(scheduler_config.to_dict())
            if scheduler_config
            else (
                self._manager.scheduler_config
                if self._manager and hasattr(self._manager, "scheduler_config")
                else SchedulerConfig.load()
            )
        )

        self._interfaces: list[NetworkInterfaceInfo] = []
        self._tabs = QTabWidget()
        # Nine tabs across the top either scroll their titles out of sight or wrap onto a
        # second row that pushes the content down. Moving the bar to the West side is not
        # enough on its own: a vertical QTabBar whose tabs are too narrow *rotates the
        # labels 90 degrees* and still clips them, so the bar is hidden and a plain
        # QListWidget is used as the navigator instead.
        #
        # `_tabs` still owns the pages and the current index, so every existing caller
        # (setCurrentIndex, widget(i), tabText(i), count()) keeps working unchanged.
        self._tabs.setDocumentMode(True)
        self._tabs.tabBar().hide()

        self._setup_ui()
        if self._manager and hasattr(self._manager, "animepahe_status_changed"):
            self._manager.animepahe_status_changed.connect(self._on_animepahe_status_changed)
        if self._manager and hasattr(self._manager, "animepahe_queue_changed"):
            self._manager.animepahe_queue_changed.connect(self._on_animepahe_queue_changed)
        self._populate_fields()
        self._restore_size_from_db()
        self._start_probe()

        self._initial_tab = tab_index(initial_tab) if isinstance(initial_tab, str) else int(initial_tab)
        if 0 <= self._initial_tab < self._tabs.count():
            self._tabs.setCurrentIndex(self._initial_tab)

    def current_tab_name(self) -> str:
        """Name of the page currently shown, e.g. ``"tor"``. ``""`` if there are none."""
        index = self._tabs.currentIndex()
        if 0 <= index < len(self._tab_names):
            return self._tab_names[index]
        return ""

    def _start_probe(self):
        """Kick off the blocking system probes on a worker thread.

        Called at the end of ``__init__`` so the work overlaps with the dialog's remaining
        construction rather than adding to the time before it appears.

        A plain daemon thread rather than a QThread, deliberately: see `_PrefsProbeEmitter` for why
        an interruptible-looking `QThread` here is a process-abort risk. If a probe is already in
        flight this does nothing - the earlier one will report.
        """
        if getattr(self, "_probe_worker", None) is not None:
            return
        emitter = _PrefsProbeEmitter()  # unparented - see _PrefsProbeEmitter
        self._probe_emitter = emitter
        emitter.finished.connect(self._apply_probe_results)
        worker = _PrefsProbeWorker(
            emitter,
            tor_hint=(getattr(self._tor_cfg, "tor_executable_path", "") or ""),
        )
        self._probe_worker = worker
        worker.start()

    def _apply_probe_results(self, results):
        """Fill in everything the probe found. Runs on the GUI thread.

        Every value is written unconditionally from the *probed* result rather than being folded
        into the current widget state, so this cannot overwrite something the user typed while the
        probe was in flight - there is nothing of theirs here, only what the system reports.

        Each section is guarded independently: one failure must not stop the rest from being
        applied, and a widget that does not exist on this build must not raise.
        """
        self._probe_worker = None

        interfaces = results.get("interfaces")
        if isinstance(interfaces, list):
            self._apply_interfaces(interfaces)

        tor = results.get("tor")
        if tor is not None:
            # Only prefill when the user has no explicit path. `_populate_fields` has already
            # loaded the saved value by now, so an empty box means "not configured".
            if not self._tor_path_edit.text().strip():
                self._tor_path_edit.setText(tor)

        ytdlp_path = results.get("ytdlp_path", "")
        version = results.get("ytdlp_version", "")
        if ytdlp_path:
            self._yt_path_status.setText(f"✓ {version}" if version else "✓ found")
            self._yt_path_status.setStyleSheet("color: #3fb950;")
            self._yt_version_lbl.setText(f"yt-dlp {version or 'unknown'}")
        else:
            self._yt_path_status.setText("✗ not found — pip install -U yt-dlp")
            self._yt_path_status.setStyleSheet("color: #f85149;")
            self._yt_version_lbl.setText("yt-dlp unavailable")

        if results.get("ffmpeg_path"):
            self._yt_ffmpeg_status.setText("✓ found")
            self._yt_ffmpeg_status.setStyleSheet("color: #3fb950;")
        else:
            self._yt_ffmpeg_status.setText("✗ not found")
            self._yt_ffmpeg_status.setStyleSheet("color: #f85149;")


    def _get_db(self) -> Optional[Database]:
        if self._db is not None:
            return self._db
        parent = self.parent()
        if parent:
            mgr = getattr(parent, "_manager", None)
            if mgr and getattr(mgr, "_db", None):
                self._db = mgr._db
                return self._db
            db = getattr(parent, "_db", None)
            if db:
                self._db = db
                return self._db
        try:
            db = Database()
            db.open()
            self._db = db
            return self._db
        except Exception:
            return None

    def _build_tab_body(self) -> QWidget:
        """Sidebar navigator on the left, tab pages on the right, in one row.

        A ``QListWidget`` rather than a vertical ``QTabBar``: Qt rotates a vertical tab
        bar's labels 90 degrees whenever the tab is narrower than its text, which is most
        of these titles, and clips whatever does not fit. A list draws horizontal text at a
        width we choose, scrolls when the list is longer than the dialog, and keeps the
        emoji icons.

        Both directions are kept in sync: clicking a row switches page, and switching page
        (including ``setCurrentIndex`` from ``initial_tab``) moves the selection. Signals
        are blocked on the programmatic half to stop the two bouncing off each other.
        """
        body = QWidget()
        row = QHBoxLayout(body)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._tab_sidebar = QListWidget()
        self._tab_sidebar.setObjectName("preferencesSidebar")
        self._tab_sidebar.setFixedWidth(self._sidebar_width())
        self._tab_sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tab_sidebar.setSpacing(1)
        for i in range(self._tabs.count()):
            text = self._tabs.tabText(i)
            item = QListWidgetItem(text)
            # Split off the leading emoji so it renders at a consistent size rather than
            # as a full-height glyph next to 11px text.
            if " " in text:
                icon, _, label = text.partition(" ")
                item.setText(label)
                item.setIcon(self._emoji_icon(icon))
                item.setData(Qt.ItemDataRole.UserRole, label)
            else:
                item.setData(Qt.ItemDataRole.UserRole, text)
            self._tab_sidebar.addItem(item)
        self._tab_sidebar.setCurrentRow(0)

        row.addWidget(self._tab_sidebar)
        row.addWidget(self._tabs, 1)

        self._tab_sidebar.currentRowChanged.connect(self._on_sidebar_row_changed)
        self._tabs.currentChanged.connect(self._on_tab_current_changed)
        return body

    def _emoji_icon(self, emoji: str):
        from PySide6.QtGui import QPixmap

        from my_idm.utils import create_emoji_icon

        try:
            return create_emoji_icon(emoji, size=16)
        except Exception:
            return QIcon()

    def _sidebar_width(self) -> int:
        """Wide enough for the longest label plus its icon, capped so it cannot dominate."""
        metrics = QFontMetrics(self.font())
        labels = []
        for i in range(self._tabs.count()):
            text = self._tabs.tabText(i)
            labels.append(text.partition(" ")[2] or text)
        widest = max((metrics.horizontalAdvance(t) for t in labels), default=120)
        return max(170, min(widest + 62, 300))

    def _on_sidebar_row_changed(self, row: int) -> None:
        if row >= 0 and row != self._tabs.currentIndex():
            self._tabs.setCurrentIndex(row)

    def _on_tab_current_changed(self, index: int) -> None:
        if index < 0:
            return
        if self._tab_sidebar.currentRow() != index:
            self._tab_sidebar.blockSignals(True)
            try:
                self._tab_sidebar.setCurrentRow(index)
            finally:
                self._tab_sidebar.blockSignals(False)

    def _restore_size_from_db(self):
        """Restore preferences window dimensions from database or QSettings."""
        try:
            db = self._get_db()
            if db:
                size_data = db.get_preferences_window_size()
                if size_data and isinstance(size_data, dict):
                    w = size_data.get("width")
                    h = size_data.get("height")
                    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                        self.resize(max(w, 740), max(h, 560))
                        return
                elif self._db is not None:
                    # Explicit DB provided with no saved size yet; use default
                    self.resize(820, 600)
                    return
            settings = QSettings("MyIDM", "My-IDM")
            w = settings.value("preferences_dialog_width", type=int)
            h = settings.value("preferences_dialog_height", type=int)
            if w and h and w > 0 and h > 0:
                self.resize(max(w, 740), max(h, 560))
                return
        except Exception as exc:
            log.warning("Failed to restore preferences dialog size from DB: %s", exc)

        # Default widened size (820px width vs original 640px)
        self.resize(820, 600)

    def _save_size_to_db(self):
        """Persist preferences window dimensions to database and QSettings."""
        try:
            w = self.width()
            h = self.height()
            if w > 0 and h > 0:
                db = self._get_db()
                if db:
                    db.save_preferences_window_size(w, h)
                settings = QSettings("MyIDM", "My-IDM")
                settings.setValue("preferences_dialog_width", w)
                settings.setValue("preferences_dialog_height", h)
        except Exception as exc:
            log.warning("Failed to save preferences dialog size to DB: %s", exc)

    def done(self, result: int):
        self._save_size_to_db()
        super().done(result)

    def closeEvent(self, event):
        self._save_size_to_db()
        # Drop the probe's result connection before the dialog goes.
        #
        # The worker thread cannot be interrupted and is left to finish on its own; it is a daemon
        # holding a reference only to its own emitter, which this detaches. With the receiver gone a
        # late result is delivered to nobody, which is the correct outcome for a dialog that has
        # closed rather than something to be reported.
        emitter = getattr(self, "_probe_emitter", None)
        if emitter is not None:
            try:
                emitter.finished.disconnect(self._apply_probe_results)
            except (RuntimeError, TypeError):
                pass  # already disconnected, or the C++ object is gone
            self._probe_emitter = None
        super().closeEvent(event)

    # -----------------------------------------------------------------------
    # UI Setup
    # -----------------------------------------------------------------------

    @staticmethod
    def _wrap_scrollable(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(content)
        return scroll

    def _setup_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setSpacing(14)
        root_layout.setContentsMargins(18, 18, 18, 18)

        # Tabs. The builder list, the insertion order and the sidebar titles all come from
        # TAB_ORDER / TAB_TITLES, so `tab_index()` cannot point at the wrong page.
        self._tab_builders = (
            (TAB_GENERAL, self._create_general_tab),
            (TAB_APP, self._create_app_tab),
            (TAB_CLIPBOARD, self._create_clipboard_tab),
            (TAB_VIEWS, self._create_views_tab),
            (TAB_TORRENT, self._create_torrent_tab),
            (TAB_BROWSER, self._create_browser_tab),
            (TAB_VPN, self._create_vpn_tab),
            (TAB_TOR, self._create_tor_tab),
            (TAB_SECURITY, self._create_security_tab),
            (TAB_EXTERNAL_TOOLS, self._create_external_tools_tab),
            (TAB_YOUTUBE, self._create_youtube_tab),
            (TAB_QUEUES, self._create_queues_tab),
            (TAB_BANDWIDTH, self._create_bandwidth_tab),
            (TAB_SCHEDULER, self._create_scheduler_tab),
        )
        self._tab_names: list[str] = []
        for name, builder in self._tab_builders:
            self._tab_names.append(name)
            self._tabs.addTab(self._wrap_scrollable(builder()), TAB_TITLES[name])
        root_layout.addWidget(self._build_tab_body())

        # Dialog Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        self._save_btn = QPushButton("Save Settings")
        self._save_btn.setObjectName("primaryButton")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(self._save_btn)

        root_layout.addLayout(btn_layout)

    def _create_views_tab(self) -> QWidget:
        """Segregated-view grouping plus which columns the downloads table shows, and in what order.

        The column half is a UI over state the table already owns: visibility and order
        live in the QHeaderView's own state, which ``MainWindow._save_ui_state_to_db()``
        already persists as a ``header_state`` blob. So this tab never invents a second
        source of truth - it reads the header, lets the user edit it, writes it straight
        back, and lets the existing save path persist it.
        """
        from my_idm.download_model import (
            SEGREGATED_MODES,
            SEGREGATED_MODE_LABELS,
        )

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # -- Appearance --------------------------------------------------------
        # Its own group, because it repaints the app and is not a table-section setting.
        #
        # Kept deliberately compact. This is the tallest page in the dialog, so a group box
        # with the default spacing costs ~78px of vertical space - and every pixel it takes
        # comes out of the column list below, which then hides columns and grows a scrollbar.
        # That is the same defect as the cropping it was meant to avoid, so the margins and
        # spacing are squeezed rather than the list being shrunk.
        theme_group = QGroupBox("Appearance")
        theme_layout = QHBoxLayout(theme_group)
        theme_layout.setContentsMargins(8, 0, 8, 0)
        theme_layout.setSpacing(8)
        self._theme_combo = QComboBox()
        for theme_id in THEME_NAMES:
            self._theme_combo.addItem(THEME_LABELS[theme_id], theme_id)
        self._theme_combo.setToolTip(
            "Applies immediately - no need to restart. The Dark theme is the app's "
            "original appearance."
        )
        theme_layout.addWidget(QLabel("Theme:"))
        theme_layout.addWidget(self._theme_combo, 1)
        layout.addWidget(theme_group)

        # -- Segregated view ---------------------------------------------------
        seg_group = QGroupBox("Segregated View")
        seg_layout = QVBoxLayout(seg_group)

        self._seg_enabled_cb = QCheckBox("Group downloads into sections")
        self._seg_enabled_cb.setToolTip(
            "Splits the table into collapsible sections. Choose what the sections group by "
            "below. The View menu can also toggle this at any time."
        )
        seg_layout.addWidget(self._seg_enabled_cb)

        self._seg_mode_combo = QComboBox()
        for mode in SEGREGATED_MODES:
            self._seg_mode_combo.addItem(SEGREGATED_MODE_LABELS[mode], mode)
        self._seg_mode_combo.setToolTip(
            "Status groups Active / Seeding / Inactive. Date groups Today / Yesterday / "
            "Last 7 Days / Last 30 Days / Older. File Type groups Video / Audio / Archives "
            "/ Documents / Photos / General."
        )
        seg_layout.addWidget(self._seg_mode_combo)
        layout.addWidget(seg_group)

        # -- Columns -----------------------------------------------------------
        col_group = QGroupBox("Downloads Table Columns")
        col_layout = QVBoxLayout(col_group)
        # The list sizes itself (Fixed height, a whole number of rows), so it cannot absorb
        # spare space the way a stretched widget does. A QVBoxLayout with nothing stretchable
        # *centres* its items in the leftover, which put a ~46px gap above the first row and
        # ~37px below the last, with the group frame drawn around the empty space.
        # SetAlignment(AlignTop) packs everything to the top, and SetMinimumSize makes the
        # layout report its minimum as its preferred size so the group hugs its content.
        col_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        col_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        col_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        # Word-wrap the hint. A plain QLabel in a QVBoxLayout never wraps, so in a narrow
        # dialog the sentence is clipped at the right edge mid-word ("Use the arrow…"),
        # which reads as a cropped control rather than as a truncated line of help text.
        col_hint = QLabel(
            "Tick a column to show it. Use the arrows to change the left-to-right order."
        )
        col_hint.setWordWrap(True)
        col_layout.addWidget(col_hint)

        # List on the left, actions stacked on the right. The horizontal layout matters:
        # with the buttons underneath, the list inherited the group's full stretch and the
        # long names ("Seeding Started At", "File / Folder Name") either overflowed the row
        # or forced a horizontal scrollbar, which read as a broken control.
        col_body = QHBoxLayout()
        self._column_list = _WholeRowListWidget()
        self._column_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._column_list.setUniformItemSizes(True)
        # Elide rather than scroll sideways or clip mid-glyph: a column name that cannot
        # be fully shown is still identifiable from its start plus the tooltip.
        self._column_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._column_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._column_list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel
        )
        # Enough room for the longest header plus the checkbox, so nothing is elided at a
        # normal window width.
        # Fixed height: the widget sizes itself to a whole multiple of the row height, so
        # there is no minimum to set and nothing here can trade rows away for space.
        self._column_list.setMinimumWidth(260)
        # The first row was clipped against the viewport edge. `uniformItemSizes` makes Qt
        # size every row from the first one it measures, so without a margin the top row
        # renders half a line high and "#" reads as a smudge.
        self._column_list.setViewportMargins(0, 4, 0, 4)
        col_body.addWidget(self._column_list, 1)

        btn_col = QVBoxLayout()
        self._col_up_btn = QPushButton("▲  Move Up")
        self._col_down_btn = QPushButton("▼  Move Down")
        self._col_reset_btn = QPushButton("↺  Reset")
        self._col_reset_btn.setToolTip(
            "Show every column and restore the default left-to-right order, widths, "
            "sorting and filters"
        )
        for btn in (self._col_up_btn, self._col_down_btn, self._col_reset_btn):
            btn_col.addWidget(btn)
        btn_col.addStretch()
        col_body.addLayout(btn_col)
        # Match the list's top margin so the buttons do not sit above the first row.
        col_body.insertSpacing(0, 0)
        col_body.setContentsMargins(0, 0, 0, 0)
        col_layout.addSpacing(4)
        col_layout.addLayout(col_body, 1)
        layout.addWidget(col_group, 1)

        self._col_up_btn.clicked.connect(lambda: self._move_selected_column(-1))
        self._col_down_btn.clicked.connect(lambda: self._move_selected_column(1))
        self._col_reset_btn.clicked.connect(self._reset_columns_to_defaults)
        self._seg_enabled_cb.toggled.connect(self._sync_seg_controls)

        self._populate_views_tab()
        return tab

    # -- Views tab: population ------------------------------------------------

    def _table_view(self):
        """The downloads table, or None when the dialog is standalone.

        ``SettingsDialog`` is constructed standalone by tests and by any future headless
        use, so every view control has to work - and simply apply nothing - without a
        parent window.
        """
        parent = self.parent()
        table = getattr(parent, "_table", None)
        if table is None or getattr(table, "horizontalHeader", None) is None:
            return None
        return table

    def _sync_seg_controls(self, checked: bool | None = None):
        """Keep the Segregated View controls consistent with the enable checkbox.

        The mode combo (Status / Date / File Type) is only meaningful while the table is actually
        grouped into sections, so it follows the checkbox. Populating the page and clicking the
        checkbox both route through here rather than setting the enabled state directly, so the two
        cannot disagree - a combo that was enabled-when-off on a freshly opened dialog would
        silently let the user pick a mode that does nothing.

        Mirrors ``MainWindow._sync_segregation_mode_actions``, which does the same job for the View
        menu's three actions.
        """
        if checked is None:
            checked = self._seg_enabled_cb.isChecked()
        self._seg_mode_combo.setEnabled(bool(checked))

    def _populate_views_tab(self) -> None:
        from my_idm.download_model import (
            DEFAULT_SEGREGATED_MODE,
            SEGREGATED_MODES,
        )

        # `_get_db()`, not `self._db`: the raw attribute is still None this early in construction
        # (the Views page is built before anything resolves it), so reading it here silently skipped
        # the database and left the Segregated View checkbox at its default - so the page opened
        # showing "off" for a saved "on", and with it the mode combo's enabled state wrong too.
        db = self._get_db()
        if db is not None:
            self._seg_enabled_cb.setChecked(bool(db.get_ui_state("segregated_view_enabled", False)))
            mode = db.get_ui_state("segregated_view_mode", DEFAULT_SEGREGATED_MODE)
            if mode not in SEGREGATED_MODES:
                mode = DEFAULT_SEGREGATED_MODE
            index = self._seg_mode_combo.findData(mode)
            self._seg_mode_combo.setCurrentIndex(max(0, index))
        # The mode choices are meaningless while segregation is off, so they follow the checkbox -
        # the same coupling `_seg_enabled_cb.toggled` maintains interactively (see `_build_views_tab`).
        self._sync_seg_controls()

        # The combo, not the running palette, is the source of truth for what the user
        # picked: reading `current_theme()` back would report the *applied* theme and so
        # could never show a selection that is pending.
        stored = db.get_ui_state("theme", DEFAULT_THEME) if db is not None else DEFAULT_THEME
        self._theme_combo.setCurrentIndex(
            max(0, self._theme_combo.findData(normalize_theme(stored)))
        )

        self._column_list.clear()
        table = self._table_view()
        header = table.horizontalHeader() if table is not None else None
        from my_idm.download_model import Col

        # Walk *visual* positions and ask for the logical column sitting there, so the list
        # reads left-to-right exactly as the table renders. Iterating logical indices and
        # trying to place each one at its visual slot drops columns, because a slot is
        # already occupied by a column that has not been placed yet.
        for visual in range(Col.COUNT):
            logical = header.logicalIndex(visual) if header is not None else visual
            if logical < 0:
                continue
            item = QListWidgetItem(Col.HEADERS[logical])
            item.setData(Qt.ItemDataRole.UserRole, logical)
            # QListWidgetItem.setToolTip takes a single string, unlike QWidget's two-arg
            # overload. The item may be elided in a narrow dialog, so the tooltip carries
            # the full name and what the tick does.
            item.setToolTip(f"{Col.HEADERS[logical]} — shown in the downloads table")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            hidden = header.isSectionHidden(logical) if header is not None else False
            item.setCheckState(
                Qt.CheckState.Unchecked if hidden else Qt.CheckState.Checked
            )
            self._column_list.addItem(item)

        # Now that the items exist the real row height is known, so the list can size
        # itself to a whole number of rows.
        self._column_list.relayout_rows()

    def _move_selected_column(self, delta: int) -> None:
        row = self._column_list.currentRow()
        if row < 0:
            return
        target = row + delta
        if target < 0 or target >= self._column_list.count():
            return
        item = self._column_list.takeItem(row)
        self._column_list.insertItem(target, item)
        self._column_list.setCurrentRow(target)

    def _reset_columns_to_defaults(self) -> None:
        from my_idm.download_model import Col

        for position in range(self._column_list.count()):
            item = self._column_list.item(position)
            item.setCheckState(Qt.CheckState.Checked)
            item.setText(Col.HEADERS[item.data(Qt.ItemDataRole.UserRole)])
        self._column_list.setCurrentRow(-1)
        parent = self.parent()
        if hasattr(parent, "_on_reset_view"):
            parent._on_reset_view()
        else:
            self._populate_views_tab()

    # -- Views tab: apply -----------------------------------------------------

    def _apply_views_tab(self) -> None:
        """Push the tab's state into the table and the database.

        Column visibility and order are written straight onto the header in the order the
        list shows, using an ascending sweep. A descending sweep strands a displaced
        section near the front, which then shifts every subsequent position - the same trap
        ``MainWindow._on_reset_view`` documents.
        """
        from my_idm.download_model import (
            DEFAULT_SEGREGATED_MODE,
            SEGREGATED_MODES,
            Col,
        )

        enabled = self._seg_enabled_cb.isChecked()
        mode = self._seg_mode_combo.currentData()
        if mode not in SEGREGATED_MODES:
            mode = DEFAULT_SEGREGATED_MODE
        theme = normalize_theme(self._theme_combo.currentData())

        # The database write comes first and is unconditional, because the dialog is also
        # constructed standalone (that is how the tests use it) where there is no parent to
        # apply anything live. `_get_db()` rather than `self._db`, so the write cannot be skipped
        # outright on a construction path that has not resolved the handle yet.
        db = self._get_db()
        if db is not None:
            db.set_ui_state("segregated_view_enabled", enabled)
            db.set_ui_state("segregated_view_mode", mode)
            db.set_ui_state("theme", theme)

        parent = self.parent()
        # Applied, not just recorded, so the choice is visible before Save and the combo
        # cannot drift from what is on screen. Persisting first means the preference
        # survives even if the repaint fails.
        applied = apply_theme(QApplication.instance(), theme)
        if hasattr(parent, "_on_theme_applied"):
            parent._on_theme_applied(applied)

        table = self._table_view()
        if table is not None:
            header = table.horizontalHeader()

            wanted_hidden = set()
            order: list[int] = []
            for position in range(self._column_list.count()):
                item = self._column_list.item(position)
                logical = int(item.data(Qt.ItemDataRole.UserRole))
                order.append(logical)
                if item.checkState() == Qt.CheckState.Unchecked:
                    wanted_hidden.add(logical)

            for logical in range(Col.COUNT):
                header.setSectionHidden(logical, logical in wanted_hidden)
            for slot, logical in enumerate(order):
                visual = header.visualIndex(logical)
                if visual != slot:
                    header.moveSection(visual, slot)

        # `_on_toggle_segregated_view` is the canonical handler for the enable flag: it
        # applies it to the model, syncs the View-menu checkmark, and owns the
        # `segregated_view_enabled` DB write. It must run *before* the mode change, because
        # `_set_segregation_mode` force-enables segregation when it is currently off - the
        # previous order meant (a) saving with the box unticked silently turned segregation
        # back on and overwrote the `False` just written, and (b) unticking it did nothing
        # at all until the next restart, because `_set_segregation_mode` was checked first
        # and the `_on_toggle_segregated_view` branch behind it was unreachable.
        parent = self.parent()
        if hasattr(parent, "_on_toggle_segregated_view"):
            parent._on_toggle_segregated_view(enabled)
        if enabled and hasattr(parent, "_set_segregation_mode"):
            parent._set_segregation_mode(mode)
        if hasattr(parent, "_save_ui_state_to_db"):
            parent._save_ui_state_to_db()

    def _create_general_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. Default Download Directory
        dir_group = QGroupBox("Default Download Location")
        dir_layout = QVBoxLayout(dir_group)
        dir_layout.setSpacing(10)

        path_row = QHBoxLayout()
        self._save_path_edit = QLineEdit()
        self._save_path_edit.setPlaceholderText(DEFAULT_DOWNLOADS_DIR)
        path_row.addWidget(self._save_path_edit, 1)

        browse_btn = QPushButton("Browse …")
        browse_btn.clicked.connect(self._on_browse_default_path)
        path_row.addWidget(browse_btn)

        open_folder_btn = QPushButton("📁 Open Folder")
        open_folder_btn.setToolTip("Open this download directory in File Explorer")
        open_folder_btn.clicked.connect(self._on_open_default_path)
        path_row.addWidget(open_folder_btn)

        dir_layout.addLayout(path_row)

        self._remember_last_cb = QCheckBox(
            "Remember last used folder when adding downloads"
        )
        self._remember_last_cb.setToolTip(
            "When checked, choosing a different folder in the Add Download dialog "
            "will be automatically used for subsequent downloads."
        )
        dir_layout.addWidget(self._remember_last_cb)

        layout.addWidget(dir_group)

        # 2. Performance & Engine Defaults
        perf_group = QGroupBox("Download Performance && Engine Defaults")
        perf_layout = QVBoxLayout(perf_group)
        perf_layout.setSpacing(10)

        seg_row = QHBoxLayout()
        seg_lbl = QLabel("Default parallel connections (segments) for HTTP:")
        seg_row.addWidget(seg_lbl, 1)
        self._segments_spin = QSpinBox()
        self._segments_spin.setRange(1, 32)
        self._segments_spin.setToolTip("Number of parallel connection streams per HTTP download")
        seg_row.addWidget(self._segments_spin)
        perf_layout.addLayout(seg_row)

        stagger_row = QHBoxLayout()
        stagger_lbl = QLabel("Delay between starting each segment:")
        stagger_row.addWidget(stagger_lbl, 1)
        self._segment_stagger_spin = QSpinBox()
        self._segment_stagger_spin.setRange(0, MAX_SEGMENT_START_DELAY_MS)
        self._segment_stagger_spin.setSingleStep(25)
        self._segment_stagger_spin.setSuffix(" ms")
        self._segment_stagger_spin.setToolTip(
            "0 starts every segment at once, which is fastest and is what most servers "
            "expect. Raise it only if a host rate-limits connection bursts and answers a "
            "starting download with 429/503: the last of N segments then waits (N-1) x this "
            "before its first request, and the step is scaled down to keep that under 2 s."
        )
        stagger_row.addWidget(self._segment_stagger_spin)
        perf_layout.addLayout(stagger_row)

        concurrent_row = QHBoxLayout()
        concurrent_lbl = QLabel("Maximum concurrent active downloads:")
        concurrent_row.addWidget(concurrent_lbl, 1)
        self._concurrent_spin = QSpinBox()
        self._concurrent_spin.setRange(1, 20)
        self._concurrent_spin.setToolTip("Maximum number of active downloads transferring simultaneously")
        concurrent_row.addWidget(self._concurrent_spin)
        perf_layout.addLayout(concurrent_row)

        layout.addWidget(perf_group)

        # 3. Retry Configuration
        retry_group = QGroupBox("Retry Configuration")
        retry_layout = QVBoxLayout(retry_group)
        retry_layout.setSpacing(10)

        retry_row = QHBoxLayout()
        retry_lbl = QLabel("Maximum automatic retries on connection failure:")
        retry_row.addWidget(retry_lbl, 1)
        self._retries_spin = QSpinBox()
        self._retries_spin.setRange(1, 20)
        self._retries_spin.setToolTip("Number of automatic reconnect attempts before marking as error")
        retry_row.addWidget(self._retries_spin)
        retry_layout.addLayout(retry_row)

        self._retry_exp_cb = QCheckBox("📈 Use exponential backoff for connection retries")
        self._retry_exp_cb.setToolTip(
            "When checked, wait time progressively increases between consecutive retry attempts "
            "to reduce server pressure and prevent spamming failed connections."
        )
        self._retry_exp_cb.toggled.connect(self._on_retry_exp_toggled)
        retry_layout.addWidget(self._retry_exp_cb)

        retry_details_layout = QHBoxLayout()
        retry_details_layout.addWidget(QLabel("Initial retry delay:"))
        self._retry_delay_spin = QDoubleSpinBox()
        self._retry_delay_spin.setRange(0.1, 120.0)
        self._retry_delay_spin.setSingleStep(0.5)
        self._retry_delay_spin.setSuffix(" sec")
        self._retry_delay_spin.setToolTip("Initial wait time before the first retry attempt (e.g. 2.0s)")
        retry_details_layout.addWidget(self._retry_delay_spin)

        self._retry_factor_lbl = QLabel("Multiplier:")
        retry_details_layout.addWidget(self._retry_factor_lbl)
        self._retry_factor_spin = QDoubleSpinBox()
        self._retry_factor_spin.setRange(1.0, 10.0)
        self._retry_factor_spin.setSingleStep(0.5)
        self._retry_factor_spin.setSuffix("x")
        self._retry_factor_spin.setToolTip("Factor by which delay multiplies on each retry attempt (e.g. 2.0x -> 2s, 4s, 8s, 16s...)")
        retry_details_layout.addWidget(self._retry_factor_spin)

        self._retry_max_delay_lbl = QLabel("Max cap:")
        retry_details_layout.addWidget(self._retry_max_delay_lbl)
        self._retry_max_delay_spin = QSpinBox()
        self._retry_max_delay_spin.setRange(1, 3600)
        self._retry_max_delay_spin.setSingleStep(10)
        self._retry_max_delay_spin.setSuffix(" sec")
        self._retry_max_delay_spin.setToolTip("Maximum wait time ceiling for retries")
        retry_details_layout.addWidget(self._retry_max_delay_spin)

        retry_layout.addLayout(retry_details_layout)

        layout.addWidget(retry_group)

        # 4. Backlog Auto-Processing Locations
        backlog_group = QGroupBox("Backlog Files Auto-Processing")
        backlog_layout = QVBoxLayout(backlog_group)
        backlog_layout.setSpacing(8)

        backlog_info_lbl = QLabel(
            "Configure folders and files to automatically scan for backlog download URLs on launch.\n"
            "By default, My-IDM scans project directory, application directory, and user home."
        )
        backlog_info_lbl.setWordWrap(True)
        backlog_info_lbl.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        backlog_layout.addWidget(backlog_info_lbl)

        self._backlog_list = QListWidget()
        self._backlog_list.setMaximumHeight(120)
        backlog_layout.addWidget(self._backlog_list)

        btn_row = QHBoxLayout()
        add_folder_btn = QPushButton("📁 Add Folder…")
        add_folder_btn.clicked.connect(self._on_add_backlog_folder)
        btn_row.addWidget(add_folder_btn)

        add_file_btn = QPushButton("📄 Add File…")
        add_file_btn.clicked.connect(self._on_add_backlog_file)
        btn_row.addWidget(add_file_btn)

        remove_btn = QPushButton("🗑 Remove")
        remove_btn.clicked.connect(self._on_remove_backlog_loc)
        btn_row.addWidget(remove_btn)

        reset_btn = QPushButton("↺ Reset Defaults")
        reset_btn.clicked.connect(self._on_reset_backlog_defaults)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()

        backlog_layout.addLayout(btn_row)

        self._clear_backlog_cb = QCheckBox(
            "Clear entries from backlog file after processing successfully"
        )
        self._clear_backlog_cb.setToolTip(
            "When checked, URLs that are successfully queued, resumed, or already in progress "
            "are removed from the backlog file to prevent duplicate processing on subsequent runs."
        )
        backlog_layout.addWidget(self._clear_backlog_cb)

        poll_row = QHBoxLayout()
        self._backlog_poll_cb = QCheckBox("Periodically scan for new backlog entries")
        self._backlog_poll_cb.setToolTip(
            "When checked, My-IDM automatically scans configured backlog folders and files "
            "for new downloads at regular intervals."
        )
        poll_row.addWidget(self._backlog_poll_cb)

        poll_lbl = QLabel("Interval:")
        poll_row.addWidget(poll_lbl)

        self._backlog_poll_spin = QSpinBox()
        self._backlog_poll_spin.setRange(5, 3600)
        self._backlog_poll_spin.setSingleStep(15)
        self._backlog_poll_spin.setSuffix(" sec")
        self._backlog_poll_spin.setToolTip("Polling frequency in seconds (default: 60s / 1 min)")
        poll_row.addWidget(self._backlog_poll_spin)
        poll_row.addStretch()

        backlog_layout.addLayout(poll_row)

        self._backlog_poll_cb.toggled.connect(self._backlog_poll_spin.setEnabled)

        layout.addWidget(backlog_group)
        layout.addStretch()
        return tab

    def _create_app_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. Application Startup & Notifications
        app_group = QGroupBox("Application Behavior")
        app_layout = QVBoxLayout(app_group)
        app_layout.setSpacing(10)

        self._auto_resume_cb = QCheckBox(
            "Automatically resume incomplete downloads when application starts"
        )
        app_layout.addWidget(self._auto_resume_cb)

        self._notify_cb = QCheckBox(
            "Show desktop / status notification when a download completes"
        )
        app_layout.addWidget(self._notify_cb)

        layout.addWidget(app_group)

        # 2. System Tray & Window Behavior
        tray_group = QGroupBox("System Tray && Window Behavior")
        tray_layout = QVBoxLayout(tray_group)
        tray_layout.setSpacing(10)

        self._enable_system_tray_cb = QCheckBox("Enable Windows system tray icon")
        self._enable_system_tray_cb.setToolTip(
            "Show an icon in the Windows notification area (system tray) with quick controls and status."
        )
        tray_layout.addWidget(self._enable_system_tray_cb)

        self._minimize_to_tray_cb = QCheckBox("Minimize window to system tray instead of taskbar")
        self._minimize_to_tray_cb.setToolTip(
            "When the window minimize button is clicked, hide the window to the system tray."
        )
        tray_layout.addWidget(self._minimize_to_tray_cb)

        self._close_to_tray_cb = QCheckBox(
            "Close window to system tray (keep downloads and seeding running in background)"
        )
        self._close_to_tray_cb.setToolTip(
            "When the window close (X) button is clicked, hide to system tray instead of terminating the app.\n"
            "Use File -> Exit or Tray Menu -> Exit to completely quit My-IDM."
        )
        tray_layout.addWidget(self._close_to_tray_cb)

        self._start_minimized_cb = QCheckBox("Start My-IDM minimized to system tray")
        self._start_minimized_cb.setToolTip(
            "Launch My-IDM directly in the background/system tray without opening the main window."
        )
        tray_layout.addWidget(self._start_minimized_cb)

        # Launch at login. The checkbox is the user's intent; `my_idm.autostart` owns the OS-level
        # registration and decides whether that intent is actually in force - the two can disagree,
        # so the status line reports the real state rather than echoing the checkbox back.
        self._launch_at_login_cb = QCheckBox("Start My-IDM when I log in")
        self._launch_at_login_cb.setToolTip(
            "Register My-IDM with the operating system so it starts when you log in.\n\n"
            "This is independent of the options above: it controls whether My-IDM is launched at "
            "all, while they control where the window goes."
        )
        tray_layout.addWidget(self._launch_at_login_cb)

        self._autostart_status_lbl = QLabel("")
        self._autostart_status_lbl.setWordWrap(True)
        self._autostart_status_lbl.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        tray_layout.addWidget(self._autostart_status_lbl)

        self._autostart_repair_btn = QPushButton("Repair Startup Entry")
        self._autostart_repair_btn.setToolTip(
            "Rewrite the login item so it points at this copy of My-IDM again.\n\n"
            "Needed after the project folder or Python environment has been moved."
        )
        self._autostart_repair_btn.clicked.connect(self._on_repair_autostart)
        self._repair_row = QHBoxLayout()
        self._repair_row.addWidget(self._autostart_repair_btn)
        self._repair_row.addStretch(1)
        tray_layout.addLayout(self._repair_row)

        self._enable_system_tray_cb.toggled.connect(self._on_system_tray_toggled)
        layout.addWidget(tray_group)

        # 3. Global Hotkey
        hotkey_group = QGroupBox("Global Hotkey")
        hotkey_layout = QVBoxLayout(hotkey_group)
        hotkey_layout.setSpacing(10)

        hotkey_row = QHBoxLayout()
        self._capture_hotkey_cb = QCheckBox("Global hotkey toggles download capture")
        self._capture_hotkey_cb.setToolTip(
            "Bind a system-wide key combination that turns browser interception and clipboard "
            "capture on and off, so capture can be silenced from any application."
        )
        hotkey_row.addWidget(self._capture_hotkey_cb)

        self._capture_hotkey_edit = QKeySequenceEdit()
        self._capture_hotkey_edit.setMaximumWidth(160)
        self._capture_hotkey_edit.setToolTip(
            "The key combination to claim system-wide. It must include Ctrl, Alt or Win — a "
            "bare key would swallow that key in every other application."
        )
        hotkey_row.addStretch(1)
        hotkey_row.addWidget(self._capture_hotkey_edit)
        hotkey_layout.addLayout(hotkey_row)

        self._capture_hotkey_status_lbl = QLabel("")
        self._capture_hotkey_status_lbl.setWordWrap(True)
        self._capture_hotkey_status_lbl.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        hotkey_layout.addWidget(self._capture_hotkey_status_lbl)

        self._capture_hotkey_cb.toggled.connect(self._on_capture_hotkey_toggled)
        self._capture_hotkey_edit.editingFinished.connect(
            self._on_capture_hotkey_edited
        )
        layout.addWidget(hotkey_group)
        layout.addStretch()
        return tab

    def _create_clipboard_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        clip_group = QGroupBox("Clipboard Monitoring && Filtering")
        clip_layout = QVBoxLayout(clip_group)
        clip_layout.setSpacing(12)

        self._clipboard_monitor_cb = QCheckBox(
            "Add downloads automatically when you copy one or more URLs"
        )
        self._clipboard_monitor_cb.setToolTip(
            "Watches the clipboard and adds any text whose every line is a link. "
            "Copying a single URL anywhere in My-IDM is ignored, so this never re-adds a "
            "download you just copied out of the list.\n"
            "Off by default: reading the clipboard without being asked is not something to "
            "switch on behind a user's back."
        )
        clip_layout.addWidget(self._clipboard_monitor_cb)

        limit_row = QHBoxLayout()
        limit_lbl = QLabel("Maximum URLs per copy:")
        limit_row.addWidget(limit_lbl)
        self._clipboard_max_urls_spin = QSpinBox()
        self._clipboard_max_urls_spin.setRange(1, 200)
        self._clipboard_max_urls_spin.setSuffix(" URLs")
        self._clipboard_max_urls_spin.setToolTip(
            "How many URLs one copy can add. A pasted list longer than this is truncated, so a "
            "generated list cannot become thousands of rows at once."
        )
        limit_row.addWidget(self._clipboard_max_urls_spin)
        limit_row.addStretch(1)
        clip_layout.addLayout(limit_row)

        size_row = QHBoxLayout()
        size_lbl = QLabel("Minimum file size to capture:")
        size_row.addWidget(size_lbl)
        self._clipboard_min_size_spin = QSpinBox()
        self._clipboard_min_size_spin.setRange(0, 1_048_576)
        self._clipboard_min_size_spin.setSingleStep(256)
        self._clipboard_min_size_spin.setSuffix(" KB")
        self._clipboard_min_size_spin.setSpecialValueText("0 KB (No minimum / capture all sizes)")
        self._clipboard_min_size_spin.setToolTip(
            "Ignore copied links whose resolved file is smaller than this threshold (e.g. web pages, small images, scripts).\n"
            "Each copied link is checked with the server before it is added, so this applies to the real file — "
            "including one behind a redirect or named only in a Content-Disposition header."
        )
        size_row.addWidget(self._clipboard_min_size_spin)
        size_row.addStretch(1)
        clip_layout.addLayout(size_row)

        ext_layout = QVBoxLayout()
        ext_lbl = QLabel("File extensions to ignore (comma-separated):")
        ext_layout.addWidget(ext_lbl)
        self._clipboard_ignored_exts_edit = QLineEdit()
        self._clipboard_ignored_exts_edit.setPlaceholderText("txt, htm, html, jpg, jpeg, png, gif, webp")
        self._clipboard_ignored_exts_edit.setToolTip(
            "Comma-separated list of file extensions to ignore when copying URLs (e.g. txt, htm, html, jpg, jpeg, png, gif, webp).\n"
            "Applied to both the copied link and the filename the server reports for it, so a link with "
            "no extension in its URL is still matched."
        )
        ext_layout.addWidget(self._clipboard_ignored_exts_edit)
        clip_layout.addLayout(ext_layout)

        helper_lbl = QLabel(
            "💡 Each copied link is resolved with the server before it is added, and anything that is not a "
            "file — a web page, an ignored extension, or a file below the minimum size — is skipped with a "
            "reason in the status bar rather than added as a download that would fail."
        )
        helper_lbl.setWordWrap(True)
        helper_lbl.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        clip_layout.addWidget(helper_lbl)

        self._clipboard_monitor_cb.toggled.connect(self._on_clipboard_monitor_toggled)

        layout.addWidget(clip_group)
        layout.addStretch()
        return tab

    def _create_torrent_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. Seeding & State Configuration
        seeding_group = QGroupBox("BitTorrent Seeding && State")
        seeding_layout = QVBoxLayout(seeding_group)
        seeding_layout.setSpacing(10)

        self._seeding_after_complete_cb = QCheckBox(
            "🌱 Continue seeding torrent after download finishes"
        )
        self._seeding_after_complete_cb.setToolTip(
            "When checked, completed torrents automatically transition into the 'seeding' state "
            "rather than stopping immediately."
        )
        seeding_layout.addWidget(self._seeding_after_complete_cb)

        self._resume_seeding_cb = QCheckBox(
            "🔄 Resume seeding torrents on startup"
        )
        self._resume_seeding_cb.setToolTip(
            "When checked, torrents that were in the 'seeding' status when My-IDM was closed "
            "will automatically resume seeding upon startup."
        )
        seeding_layout.addWidget(self._resume_seeding_cb)

        time_row = QHBoxLayout()
        time_lbl = QLabel("Maximum seeding duration:")
        time_row.addWidget(time_lbl, 1)
        self._seeding_time_spin = QSpinBox()
        self._seeding_time_spin.setRange(0, 525_600)  # Up to 1 year in minutes
        self._seeding_time_spin.setSingleStep(15)
        self._seeding_time_spin.setSuffix(" min")
        self._seeding_time_spin.setSpecialValueText("Unlimited (Indefinite)")
        self._seeding_time_spin.setToolTip(
            "Automatically stop seeding after the torrent has been seeding for this many minutes.\n"
            "Set to 0 to seed indefinitely."
        )
        time_row.addWidget(self._seeding_time_spin)
        seeding_layout.addLayout(time_row)

        ratio_limit_row = QHBoxLayout()
        ratio_limit_lbl = QLabel("Maximum share ratio limit:")
        ratio_limit_row.addWidget(ratio_limit_lbl, 1)
        self._seeding_ratio_limit_spin = QDoubleSpinBox()
        self._seeding_ratio_limit_spin.setRange(0.0, 100.0)
        self._seeding_ratio_limit_spin.setSingleStep(0.1)
        self._seeding_ratio_limit_spin.setSuffix(" x")
        self._seeding_ratio_limit_spin.setSpecialValueText("Unlimited (0.0x)")
        self._seeding_ratio_limit_spin.setToolTip(
            "Automatically stop seeding when the upload to download share ratio reaches this limit.\n"
            "Set to 0.0 for unlimited share ratio."
        )
        ratio_limit_row.addWidget(self._seeding_ratio_limit_spin)
        seeding_layout.addLayout(ratio_limit_row)

        speed_row = QHBoxLayout()
        speed_lbl = QLabel("Maximum upload / seeding speed limit:")
        speed_row.addWidget(speed_lbl, 1)
        self._max_seeding_speed_spin = QSpinBox()
        self._max_seeding_speed_spin.setRange(0, 10_000_000)
        self._max_seeding_speed_spin.setSingleStep(10)
        self._max_seeding_speed_spin.setSuffix(" KB/s")
        self._max_seeding_speed_spin.setSpecialValueText("Unlimited (0 KB/s)")
        self._max_seeding_speed_spin.setToolTip(
            "Cap the seeding upload speed in KB/s. Set to 0 for unlimited speed."
        )
        speed_row.addWidget(self._max_seeding_speed_spin)
        seeding_layout.addLayout(speed_row)

        ratio_row = QHBoxLayout()
        ratio_lbl = QLabel("Download to seeding speed ratio:")
        ratio_row.addWidget(ratio_lbl, 1)
        self._seeding_ratio_spin = QDoubleSpinBox()
        self._seeding_ratio_spin.setRange(0.1, 100.0)
        self._seeding_ratio_spin.setSingleStep(0.5)
        self._seeding_ratio_spin.setSuffix(" : 1")
        self._seeding_ratio_spin.setToolTip(
            "Ratio of download speed to seeding speed (e.g. 10.0 = 10:1 ratio, seeding is 10% of download speed).\n"
            "When a global download limit is configured, seeding upload limit is derived as:\n"
            "download limit / ratio."
        )
        ratio_row.addWidget(self._seeding_ratio_spin)
        seeding_layout.addLayout(ratio_row)

        layout.addWidget(seeding_group)

        # 2. Metadata Fetching & Timeouts
        meta_group = QGroupBox("Metadata Fetching && Timeouts")
        meta_layout = QVBoxLayout(meta_group)
        meta_layout.setSpacing(10)

        meta_row = QHBoxLayout()
        meta_lbl = QLabel("Auto-suspend BitTorrent after stuck in metadata fetch:")
        meta_row.addWidget(meta_lbl, 1)
        self._metadata_timeout_spin = QSpinBox()
        self._metadata_timeout_spin.setRange(0, 365)
        self._metadata_timeout_spin.setSingleStep(1)
        self._metadata_timeout_spin.setSuffix(" day(s)")
        self._metadata_timeout_spin.setToolTip(
            "If a BitTorrent magnet link stays stuck fetching metadata longer than "
            "this many days, it is automatically suspended. Set to 0 to disable."
        )
        meta_row.addWidget(self._metadata_timeout_spin)
        meta_layout.addLayout(meta_row)

        # -- free disk space ---------------------------------------------------
        self._disk_space_check_cb = QCheckBox(
            "Check free disk space before downloading"
        )
        self._disk_space_check_cb.setToolTip(
            "Refuse a download the target drive cannot hold, instead of letting it fail "
            "part-way through. A download that exactly fills the volume is also refused, "
            "so the disk is never left at 100%."
        )
        meta_layout.addWidget(self._disk_space_check_cb)

        headroom_row = QHBoxLayout()
        headroom_row.addWidget(QLabel("Safety margin to keep free:"), 1)
        self._disk_space_headroom_spin = QSpinBox()
        self._disk_space_headroom_spin.setRange(0, 1024 * 1024)
        self._disk_space_headroom_spin.setSingleStep(64)
        self._disk_space_headroom_spin.setSuffix(" MB")
        self._disk_space_headroom_spin.setToolTip(
            "Extra space required on top of the download size, so the page file, the "
            "recycle bin and everything else sharing the drive still have room. "
            "0 means the download must fit exactly."
        )
        headroom_row.addWidget(self._disk_space_headroom_spin)
        meta_layout.addLayout(headroom_row)
        # A margin for a check that is off is a setting that does nothing, so grey it out.
        self._disk_space_check_cb.toggled.connect(self._disk_space_headroom_spin.setEnabled)

        layout.addWidget(meta_group)

        # 3. .torrent files coming in from the OS. One group because all three settings answer
        # the same question — how does a .torrent file reach My-IDM without the Add dialog? —
        # and because the status lines underneath each are the honest answer to "did that work",
        # which a checkbox alone cannot give on any of the three platforms.
        self._create_torrent_file_integration_group(layout)

        layout.addStretch()
        return tab

    def _create_torrent_file_integration_group(self, layout):
        """Build the drag-and-drop / file-association / watched-folder controls.

        The checkbox is the user's intent; `my_idm.file_assoc` owns the OS-level registration
        and decides whether that intent is actually in force — the two can disagree, and on
        Windows they *permanently* do, because no application may set itself the default handler.
        So each status line reports the real state rather than echoing its checkbox back, which is
        the same split the launch-at-login control uses.
        """
        group = QGroupBox(".torrent Files from the System")
        gl = QVBoxLayout(group)
        gl.setSpacing(10)

        # -- file association ------------------------------------------------
        self._torrent_assoc_cb = QCheckBox("Open .torrent files with My-IDM")
        self._torrent_assoc_cb.setToolTip(
            "Register My-IDM with the operating system as a handler for .torrent files, so it "
            "appears in that file type's 'Open with' list.\n\n"
            "On Windows you will still need to choose My-IDM once in the Default Apps settings — "
            "Windows does not allow any application to make that choice for you."
        )
        gl.addWidget(self._torrent_assoc_cb)

        self._torrent_assoc_status_lbl = QLabel("")
        self._torrent_assoc_status_lbl.setWordWrap(True)
        self._torrent_assoc_status_lbl.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        gl.addWidget(self._torrent_assoc_status_lbl)

        # A "Repair" button and a "Make default" button answer two different failures: a
        # registration pointing at a moved checkout, and a correct registration the OS has not
        # made the default. Only the second one Windows can do anything about, and only by
        # sending the user there.
        self._torrent_assoc_repair_btn = QPushButton("Repair Registration")
        self._torrent_assoc_repair_btn.setToolTip(
            "Rewrite the .torrent registration so it points at this copy of My-IDM again.\n\n"
            "Needed after the project folder or Python environment has been moved."
        )
        self._torrent_assoc_repair_btn.clicked.connect(self._on_repair_torrent_assoc)
        self._torrent_assoc_default_btn = QPushButton("Open Default Apps Settings")
        self._torrent_assoc_default_btn.setToolTip(
            "Open the system settings where you choose which application opens .torrent files.\n\n"
            "Windows protects that choice from applications, so it has to be made here."
        )
        self._torrent_assoc_default_btn.clicked.connect(self._on_open_default_apps)
        assoc_row = QHBoxLayout()
        assoc_row.addWidget(self._torrent_assoc_repair_btn)
        assoc_row.addWidget(self._torrent_assoc_default_btn)
        assoc_row.addStretch(1)
        gl.addLayout(assoc_row)

        # -- watched folder ---------------------------------------------------
        self._torrent_watch_cb = QCheckBox("Watch a folder and add .torrent files that appear in it")
        self._torrent_watch_cb.setToolTip(
            "Add .torrent files automatically when they appear in the folder below.\n\n"
            "Only files modified in the last few days are considered, so pointing this at a "
            "folder that already holds a large number of torrents will not import its history."
        )
        gl.addWidget(self._torrent_watch_cb)

        folder_row = QHBoxLayout()
        self._torrent_watch_edit = QLineEdit()
        self._torrent_watch_edit.setPlaceholderText(DEFAULT_DOWNLOADS_DIR)
        self._torrent_watch_edit.setToolTip(
            "The folder to watch for .torrent files. Leave empty to watch your default "
            "download folder."
        )
        self._torrent_watch_browse_btn = QPushButton("Browse...")
        self._torrent_watch_browse_btn.clicked.connect(self._on_browse_torrent_watch_folder)
        folder_row.addWidget(self._torrent_watch_edit, 1)
        folder_row.addWidget(self._torrent_watch_browse_btn)
        gl.addLayout(folder_row)
        self._torrent_watch_clean_cb = QCheckBox("Delete .torrent files after adding (move to Trash)")
        self._torrent_watch_clean_cb.setToolTip(
            "Automatically move .torrent files to the Trash / Recycle Bin after they are "
            "picked up from the watched folder and added to downloads."
        )
        gl.addWidget(self._torrent_watch_clean_cb)

        age_row = QHBoxLayout()
        self._torrent_watch_max_age_lbl = QLabel("Ignore files older than:")
        age_row.addWidget(self._torrent_watch_max_age_lbl)
        self._torrent_watch_max_age_spin = QSpinBox()
        self._torrent_watch_max_age_spin.setRange(1, 365)
        self._torrent_watch_max_age_spin.setSuffix(" days")
        self._torrent_watch_max_age_spin.setValue(3)
        self._torrent_watch_max_age_spin.setToolTip(
            "Only add .torrent files modified within this many days.\n\n"
            "Prevents importing old history when pointing at an existing folder."
        )
        age_row.addWidget(self._torrent_watch_max_age_spin)
        age_row.addStretch(1)
        gl.addLayout(age_row)

        # Controls under watched folder are only enabled when folder watching is on.
        self._torrent_watch_cb.toggled.connect(self._on_torrent_watch_toggled)

        layout.addWidget(group)

    def _create_vpn_tab(self) -> QWidget:
        """Network adapter / VPN binding and proxy settings.

        Split out of the former combined "Network & Privacy" tab. The kill switch and
        the proxy describe the local network path, while Tor (its own tab now) is a
        routing decision that stands alone. Sharing one tab meant scrolling past three
        unrelated groups to reach the one setting being changed.
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. VPN / Adapter Binding
        iface_group = QGroupBox("Network Adapter / VPN Binding")
        iface_inner = QVBoxLayout(iface_group)
        iface_inner.setSpacing(10)

        combo_row = QHBoxLayout()
        self._iface_combo = QComboBox()
        self._iface_combo.currentIndexChanged.connect(self._on_iface_changed)
        combo_row.addWidget(self._iface_combo, 1)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.setToolTip("Rescan local network adapters")
        refresh_btn.clicked.connect(self._load_interfaces)
        combo_row.addWidget(refresh_btn)
        iface_inner.addLayout(combo_row)

        self._iface_details_label = QLabel("")
        self._iface_details_label.setWordWrap(True)
        self._iface_details_label.setStyleSheet("color: #a0aab8; font-size: 11px;")
        iface_inner.addWidget(self._iface_details_label)

        self._kill_switch_cb = QCheckBox("🔒 Enable Kill Switch")
        self._kill_switch_cb.setToolTip(
            "Prevent all downloads and traffic leaks if the VPN or bound adapter disconnects."
        )
        iface_inner.addWidget(self._kill_switch_cb)
        layout.addWidget(iface_group)

        # 2. Proxy Server Configuration
        proxy_group = QGroupBox("Proxy Server Configuration")
        proxy_inner = QVBoxLayout(proxy_group)
        proxy_inner.setSpacing(10)

        self._proxy_enable_cb = QCheckBox("🌐 Route traffic through proxy server")
        self._proxy_enable_cb.toggled.connect(self._on_proxy_toggled)
        proxy_inner.addWidget(self._proxy_enable_cb)

        type_port_row = QHBoxLayout()
        type_port_row.addWidget(QLabel("Protocol:"))
        self._proxy_type_combo = QComboBox()
        self._proxy_type_combo.addItems(["HTTP", "SOCKS5"])
        type_port_row.addWidget(self._proxy_type_combo, 1)

        type_port_row.addWidget(QLabel("Port:"))
        self._proxy_port_spin = QSpinBox()
        self._proxy_port_spin.setRange(1, 65535)
        self._proxy_port_spin.setValue(8080)
        type_port_row.addWidget(self._proxy_port_spin, 1)
        proxy_inner.addLayout(type_port_row)

        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Host / IP:"))
        self._proxy_host_edit = QLineEdit()
        self._proxy_host_edit.setPlaceholderText("e.g., 127.0.0.1 or proxy.example.com")
        host_row.addWidget(self._proxy_host_edit, 1)
        proxy_inner.addLayout(host_row)

        auth_row = QHBoxLayout()
        auth_row.addWidget(QLabel("User (opt):"))
        self._proxy_user_edit = QLineEdit()
        auth_row.addWidget(self._proxy_user_edit, 1)

        auth_row.addWidget(QLabel("Password:"))
        self._proxy_pass_edit = QLineEdit()
        self._proxy_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        auth_row.addWidget(self._proxy_pass_edit, 1)
        proxy_inner.addLayout(auth_row)

        test_net_row = QHBoxLayout()
        test_btn = QPushButton("🧪 Test Network Connection")
        test_btn.clicked.connect(self._on_test_network)
        test_net_row.addWidget(test_btn)
        test_net_row.addStretch()
        proxy_inner.addLayout(test_net_row)

        layout.addWidget(proxy_group)

        layout.addStretch()
        return tab

    def _create_tor_tab(self) -> QWidget:
        """Tor SOCKS5 routing, split out of the former combined Network & Privacy tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 3. Tor Onion Routing & Privacy
        tor_group = QGroupBox("🧅 Tor Network Privacy && Onion Routing")
        tor_inner = QVBoxLayout(tor_group)
        tor_inner.setSpacing(10)

        self._tor_enable_cb = QCheckBox("🧅 Enable Tor network routing (SOCKS5 proxy)")
        self._tor_enable_cb.setToolTip("Activate Tor proxy routing immediately")
        tor_inner.addWidget(self._tor_enable_cb)

        self._tor_autostart_cb = QCheckBox("🧅 Activate Tor automatically when My-IDM starts")
        tor_inner.addWidget(self._tor_autostart_cb)

        self._tor_route_http_cb = QCheckBox("🌐 Route standard downloads (HTTP / HTTPS) through Tor")
        self._tor_route_http_cb.setToolTip("Route direct HTTP/HTTPS web downloads through Tor SOCKS5 proxy")
        tor_inner.addWidget(self._tor_route_http_cb)

        self._tor_route_torrent_cb = QCheckBox("📦 Route BitTorrent swarms and trackers through Tor")
        self._tor_route_torrent_cb.setToolTip("Route BitTorrent peer and tracker connections through Tor SOCKS5 proxy")
        tor_inner.addWidget(self._tor_route_torrent_cb)

        tor_host_row = QHBoxLayout()
        tor_host_row.addWidget(QLabel("Tor SOCKS5 Host:"))
        self._tor_host_edit = QLineEdit("127.0.0.1")
        self._tor_host_edit.setPlaceholderText("127.0.0.1")
        tor_host_row.addWidget(self._tor_host_edit, 1)

        tor_host_row.addWidget(QLabel("Port:"))
        self._tor_port_spin = QSpinBox()
        self._tor_port_spin.setRange(1, 65535)
        self._tor_port_spin.setValue(9050)
        tor_host_row.addWidget(self._tor_port_spin)
        tor_inner.addLayout(tor_host_row)

        presets_row = QHBoxLayout()
        presets_row.addWidget(QLabel("Port Presets:"))
        preset_service_btn = QPushButton("Tor Service (Port 9050)")
        preset_service_btn.clicked.connect(lambda: self._tor_port_spin.setValue(9050))
        preset_browser_btn = QPushButton("Tor Browser (Port 9150)")
        preset_browser_btn.clicked.connect(lambda: self._tor_port_spin.setValue(9150))
        presets_row.addWidget(preset_service_btn)
        presets_row.addWidget(preset_browser_btn)
        presets_row.addStretch()
        tor_inner.addLayout(presets_row)

        test_tor_row = QHBoxLayout()
        self._tor_test_btn = QPushButton("🧪 Test Tor Connection")
        self._tor_test_btn.clicked.connect(self._on_test_tor)
        test_tor_row.addWidget(self._tor_test_btn)

        self._tor_test_status_lbl = QLabel("")
        test_tor_row.addWidget(self._tor_test_status_lbl, 1)
        tor_inner.addLayout(test_tor_row)

        # Tor Executable (Optional)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Tor Executable (opt):"))
        self._tor_path_edit = QLineEdit()
        self._tor_path_edit.setPlaceholderText("C:\\Path\\To\\tor.exe (optional)")
        path_row.addWidget(self._tor_path_edit, 1)

        browse_tor_btn = QPushButton("Browse…")
        browse_tor_btn.clicked.connect(self._on_browse_tor_path)
        path_row.addWidget(browse_tor_btn)
        tor_inner.addLayout(path_row)

        layout.addWidget(tor_group)

        layout.addStretch()
        return tab

    def _create_security_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # Pre-Download
        pre_group = QGroupBox("Pre-Download URL && Payload Safety")
        pre_inner = QVBoxLayout(pre_group)
        pre_inner.setSpacing(10)

        self._scan_before_cb = QCheckBox("🔍 Enable safety inspection before downloading starts")
        pre_inner.addWidget(self._scan_before_cb)

        self._warn_ext_cb = QCheckBox(
            "⚠️ Warn when downloading executable or script files (.exe, .msi, .bat, .vbs, .scr, .iso)"
        )
        pre_inner.addWidget(self._warn_ext_cb)

        self._block_dangerous_cb = QCheckBox(
            "🚫 Automatically block high-risk URLs (e.g. deceptive double extensions like file.pdf.exe)"
        )
        pre_inner.addWidget(self._block_dangerous_cb)

        vt_row = QHBoxLayout()
        vt_row.addWidget(QLabel("VirusTotal API Key (opt):"))
        self._vt_key_edit = QLineEdit()
        self._vt_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._vt_key_edit.setPlaceholderText("Paste your VirusTotal API key for cloud intelligence...")
        vt_row.addWidget(self._vt_key_edit, 1)
        pre_inner.addLayout(vt_row)

        layout.addWidget(pre_group)

        # Post-Download
        post_group = QGroupBox("Post-Download Antivirus Scanning")
        post_inner = QVBoxLayout(post_group)
        post_inner.setSpacing(10)

        self._scan_after_cb = QCheckBox("🛡️ Automatically scan completed files with antivirus")
        post_inner.addWidget(self._scan_after_cb)

        self._defender_rb = QRadioButton("Windows Defender (default)")
        post_inner.addWidget(self._defender_rb)

        self._custom_rb = QRadioButton("Custom Antivirus Scanner Executable")
        post_inner.addWidget(self._custom_rb)

        custom_path_row = QHBoxLayout()
        custom_path_row.addWidget(QLabel("Executable:"))
        self._custom_scanner_edit = QLineEdit()
        self._custom_scanner_edit.setPlaceholderText("C:\\Program Files\\...\\scanner.exe")
        custom_path_row.addWidget(self._custom_scanner_edit, 1)
        browse_scanner_btn = QPushButton("Browse…")
        browse_scanner_btn.clicked.connect(self._on_browse_scanner)
        custom_path_row.addWidget(browse_scanner_btn)
        post_inner.addLayout(custom_path_row)

        args_row = QHBoxLayout()
        args_row.addWidget(QLabel("Arguments:"))
        self._custom_args_edit = QLineEdit()
        self._custom_args_edit.setPlaceholderText('"%file%"')
        args_row.addWidget(self._custom_args_edit, 1)
        post_inner.addLayout(args_row)

        layout.addWidget(post_group)

        # Scan Timing
        timing_group = QGroupBox("Scan Timing")
        timing_inner = QVBoxLayout(timing_group)
        timing_inner.setSpacing(8)

        self._timing_auto_rb = QRadioButton("🔄 Automatically scan when download completes")
        timing_inner.addWidget(self._timing_auto_rb)

        self._timing_manual_rb = QRadioButton("🖱️ Manual scan only (right-click → Scan with Antivirus)")
        timing_inner.addWidget(self._timing_manual_rb)

        layout.addWidget(timing_group)

        # Threat Remediation
        action_group = QGroupBox("Action When Threat is Detected")
        action_inner = QVBoxLayout(action_group)
        self._action_warn_rb = QRadioButton("⚠️ Alert user and display threat warning (keep file)")
        action_inner.addWidget(self._action_warn_rb)
        self._action_quarantine_rb = QRadioButton("🗑️ Alert user and automatically quarantine / delete infected file")
        action_inner.addWidget(self._action_quarantine_rb)
        layout.addWidget(action_group)

        # Threat Exclusions
        excl_group = QGroupBox("Threat Exclusions (silently allowed)")
        excl_inner = QVBoxLayout(excl_group)
        excl_inner.setSpacing(8)

        excl_desc = QLabel(
            "Threats matching any pattern or category in this list (e.g. HackTool, CrackTool, Keygen) "
            "will be silently allowed without triggering warnings or quarantine actions."
        )
        excl_desc.setWordWrap(True)
        excl_desc.setStyleSheet("color: #a0aab8; font-size: 11px;")
        excl_inner.addWidget(excl_desc)

        self._threat_excl_list = QListWidget()
        self._threat_excl_list.setMaximumHeight(130)
        excl_inner.addWidget(self._threat_excl_list)

        add_row = QHBoxLayout()
        self._new_threat_excl_edit = QLineEdit()
        self._new_threat_excl_edit.setPlaceholderText("Enter threat category or pattern (e.g. Win32/Keygen, CrackTool, PUA)")
        self._new_threat_excl_edit.returnPressed.connect(self._on_add_threat_exclusion)
        add_row.addWidget(self._new_threat_excl_edit, 1)

        add_btn = QPushButton("➕ Add")
        add_btn.clicked.connect(self._on_add_threat_exclusion)
        add_row.addWidget(add_btn)

        remove_btn = QPushButton("🗑 Remove")
        remove_btn.clicked.connect(self._on_remove_threat_exclusion)
        add_row.addWidget(remove_btn)

        reset_btn = QPushButton("↺ Reset Defaults")
        reset_btn.clicked.connect(self._on_reset_threat_exclusions_defaults)
        add_row.addWidget(reset_btn)

        excl_inner.addLayout(add_row)
        layout.addWidget(excl_group)

        # Test Antivirus Scanner button
        test_scanner_btn = QPushButton("🧪 Test Antivirus Scanner")
        test_scanner_btn.clicked.connect(self._on_test_scanner)
        layout.addWidget(test_scanner_btn)

        layout.addStretch()
        return tab

    def _create_external_tools_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # AnimePahe Scraper Group
        ap_group = QGroupBox("AnimePahe Auto-Downloader / Scraper")
        ap_layout = QVBoxLayout(ap_group)
        ap_layout.setSpacing(12)

        # 1. Repository Location
        repo_lbl = QLabel("Repository Location:")
        ap_layout.addWidget(repo_lbl)

        repo_row = QHBoxLayout()
        self._animepahe_repo_edit = QLineEdit()
        self._animepahe_repo_edit.setPlaceholderText(r"e.g. D:\Projects\animepahe-downloader")
        repo_row.addWidget(self._animepahe_repo_edit, 1)

        browse_btn = QPushButton("Browse …")
        browse_btn.clicked.connect(self._on_browse_animepahe_repo)
        repo_row.addWidget(browse_btn)

        open_folder_btn = QPushButton("📁 Open Folder")
        open_folder_btn.setToolTip("Open AnimePahe repository directory in File Explorer")
        open_folder_btn.clicked.connect(self._on_open_animepahe_folder)
        repo_row.addWidget(open_folder_btn)

        ap_layout.addLayout(repo_row)

        # 2. Startup & Periodic Scheduling Options
        self._animepahe_startup_cb = QCheckBox(
            "Launch AnimePahe scraper on startup (CLI mode, forwards downloads to My-IDM backlog)"
        )
        self._animepahe_startup_cb.setToolTip(
            "When enabled, My-IDM automatically runs animepahe_download.py in CLI mode at startup.\n"
            "Discovered episodes are sent directly to the My-IDM backlog file for automatic downloading."
        )
        ap_layout.addWidget(self._animepahe_startup_cb)

        periodic_row = QHBoxLayout()
        self._animepahe_periodic_cb = QCheckBox(
            "Run AnimePahe scraper periodically in background"
        )
        self._animepahe_periodic_cb.setToolTip(
            "When enabled, My-IDM automatically runs animepahe_download.py on a recurring schedule in the background,\n"
            "checking for newly aired episodes and queuing them into the My-IDM backlog."
        )
        periodic_row.addWidget(self._animepahe_periodic_cb)

        self._animepahe_interval_lbl = QLabel("Every:")
        self._animepahe_interval_lbl.setStyleSheet("color: #8fa0b5; margin-left: 10px;")
        periodic_row.addWidget(self._animepahe_interval_lbl)

        self._animepahe_interval_spin = QSpinBox()
        self._animepahe_interval_spin.setRange(1, 168)
        self._animepahe_interval_spin.setValue(6)
        self._animepahe_interval_spin.setSuffix(" hours")
        self._animepahe_interval_spin.setToolTip("Periodic interval between automated scraper runs (default: 6 hours)")
        periodic_row.addWidget(self._animepahe_interval_spin)
        periodic_row.addStretch()

        self._animepahe_periodic_cb.toggled.connect(self._animepahe_interval_spin.setEnabled)
        self._animepahe_periodic_cb.toggled.connect(self._animepahe_interval_lbl.setEnabled)

        ap_layout.addLayout(periodic_row)

        desc_lbl = QLabel(
            "ℹ️ In CLI mode, the scraper performs an automated library check in the background. "
            "All new episodes will be queued into the backlog file and ingested automatically."
        )
        desc_lbl.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        desc_lbl.setWordWrap(True)
        ap_layout.addWidget(desc_lbl)

        # 3. Download Anime by URL & Episode Range Group
        dl_group = QGroupBox("Download Anime by URL")
        dl_layout = QVBoxLayout(dl_group)
        dl_layout.setSpacing(10)

        url_row = QHBoxLayout()
        url_lbl = QLabel("AnimePahe URL:")
        url_lbl.setMinimumWidth(110)
        url_row.addWidget(url_lbl)

        self._animepahe_url_edit = QLineEdit()
        self._animepahe_url_edit.setPlaceholderText(
            "e.g. https://animepahe.ru/anime/4380 or https://animepahe.si/anime/ef667bb4-3a9b-449e-1a22-26156a642e47"
        )
        self._animepahe_url_edit.setClearButtonEnabled(True)
        self._animepahe_url_edit.returnPressed.connect(self._on_download_animepahe_url)
        url_row.addWidget(self._animepahe_url_edit, 1)
        dl_layout.addLayout(url_row)

        options_row = QHBoxLayout()

        ep_lbl = QLabel("Episode Range:")
        ep_lbl.setMinimumWidth(110)
        options_row.addWidget(ep_lbl)

        self._animepahe_episodes_edit = QLineEdit()
        self._animepahe_episodes_edit.setPlaceholderText("e.g. 1-12, 15, 20-25 (optional, leave blank for all)")
        self._animepahe_episodes_edit.setClearButtonEnabled(True)
        self._animepahe_episodes_edit.returnPressed.connect(self._on_download_animepahe_url)
        options_row.addWidget(self._animepahe_episodes_edit, 1)

        q_lbl = QLabel("Quality:")
        options_row.addWidget(q_lbl)
        self._animepahe_quality_combo = QComboBox()
        self._animepahe_quality_combo.addItems(["Auto", "1080p", "720p", "360p"])
        options_row.addWidget(self._animepahe_quality_combo)

        l_lbl = QLabel("Audio:")
        options_row.addWidget(l_lbl)
        self._animepahe_lang_combo = QComboBox()
        self._animepahe_lang_combo.addItems(["Auto", "Sub (jap)", "Dub (en)"])
        options_row.addWidget(self._animepahe_lang_combo)

        self._btn_download_animepahe_url = QPushButton("⬇️ Download via AnimePahe")
        self._btn_download_animepahe_url.setObjectName("primaryButton")
        self._btn_download_animepahe_url.setToolTip(
            "Launch AnimePahe scraper to resolve streams and forward download jobs to My-IDM"
        )
        self._btn_download_animepahe_url.clicked.connect(self._on_download_animepahe_url)
        options_row.addWidget(self._btn_download_animepahe_url)

        self._animepahe_queue_lbl = QLabel("")
        self._animepahe_queue_lbl.setStyleSheet("color: #00d2ff; font-weight: bold; font-size: 11px; margin-left: 8px;")
        self._animepahe_queue_lbl.setVisible(False)
        options_row.addWidget(self._animepahe_queue_lbl)

        dl_layout.addLayout(options_row)

        dl_desc = QLabel(
            "ℹ️ AnimePahe will resolve the stream links, bypass Cloudflare/Kwik, and automatically push the episodes "
            "into My-IDM's backlog queue for accelerated multi-segmented download."
        )
        dl_desc.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        dl_desc.setWordWrap(True)
        dl_layout.addWidget(dl_desc)

        ap_layout.addWidget(dl_group)

        # 4. Logs & Actions Group
        logs_group = QGroupBox("Diagnostics && Logs")
        logs_layout = QVBoxLayout(logs_group)
        logs_layout.setSpacing(8)

        log_btns_layout = QHBoxLayout()

        self._btn_view_console_log = QPushButton("📄 View Console Logs")
        self._btn_view_console_log.setToolTip("Open CLI stdout/stderr redirection log file")
        self._btn_view_console_log.clicked.connect(self._on_view_animepahe_console_log)
        log_btns_layout.addWidget(self._btn_view_console_log)

        self._btn_view_debug_log = QPushButton("🔍 View Debug Logs")
        self._btn_view_debug_log.setToolTip("Open AnimePahe debug_log.txt")
        self._btn_view_debug_log.clicked.connect(self._on_view_animepahe_debug_log)
        log_btns_layout.addWidget(self._btn_view_debug_log)

        self._btn_run_cli_now = QPushButton("▶️ Run CLI Now")
        self._btn_run_cli_now.setToolTip("Launch AnimePahe background scraper in CLI mode")
        self._btn_run_cli_now.clicked.connect(self._on_run_animepahe_cli_from_settings)
        log_btns_layout.addWidget(self._btn_run_cli_now)

        self._btn_launch_gui_now = QPushButton("🎬 Launch GUI Now")
        self._btn_launch_gui_now.setToolTip("Launch AnimePahe standalone desktop interface")
        self._btn_launch_gui_now.clicked.connect(self._on_launch_animepahe_gui_from_settings)
        log_btns_layout.addWidget(self._btn_launch_gui_now)

        logs_layout.addLayout(log_btns_layout)
        ap_layout.addWidget(logs_group)

        layout.addWidget(ap_group)
        layout.addStretch()
        return tab


    def _create_queues_tab(self) -> QWidget:
        """Queue Manager - create, rename, reorder, limit and delete named queues."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # Queue Manager group
        queues_group = QGroupBox("Queue Manager")
        queues_layout = QVBoxLayout(queues_group)
        queues_layout.setSpacing(14)
        queues_layout.setContentsMargins(14, 16, 14, 14)

        # The Queue Manager table
        self._queues_table = QTableWidget(0, 5, self)
        self._queues_table.setHorizontalHeaderLabels([
            "Queue",
            "Downloads",
            "Max at once\n(0 = Global)",
            "Download limit\n(0 = Global)",
            "Upload limit\n(0 = Global)",
        ])
        # Two lines per header
        _header = self._queues_table.horizontalHeader()
        _line_height = _header.fontMetrics().height()
        _header.setFixedHeight(_line_height * 2 + 12)
        # Reset padding
        themed_widget(
            _header,
            """
            QHeaderView::section {
                background-color: {Colors.BG_MID};
                color: {Colors.TEXT_SECONDARY};
                border: none;
                border-bottom: 2px solid {Colors.BORDER};
                border-right: 1px solid {Colors.BORDER};
                padding: 6px 10px;
                font-weight: 600;
                font-size: 12px;
                text-transform: uppercase;
            }
            QHeaderView::section:hover {
                color: {Colors.TEXT};
                background-color: {Colors.BG_LIGHT};
            }
            """,
        )
        self._queues_table.verticalHeader().setVisible(False)
        self._queues_table.verticalHeader().setDefaultSectionSize(34)
        self._queues_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._queues_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._queues_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._queues_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self._queues_table.itemSelectionChanged.connect(self._on_queues_selection_changed)
        queues_layout.addWidget(self._queues_table)

        # Note explaining how queue limits interact with global limits
        self._queues_note = QLabel()
        self._queues_note.setWordWrap(True)
        self._queues_note.setStyleSheet("color: #8fa0b5;")
        queues_layout.addWidget(self._queues_note)

        # Buttons
        btn_row = QHBoxLayout()
        self._queues_up_btn = QPushButton("↑ Move Up")
        self._queues_down_btn = QPushButton("↓ Move Down")
        self._queues_add_btn = QPushButton("Add…")
        self._queues_rename_btn = QPushButton("Rename…")
        self._queues_color_btn = QPushButton("Color…")
        self._queues_delete_btn = QPushButton("Delete…")
        self._queues_up_btn.clicked.connect(lambda: self._move_selected_queue(-1))
        self._queues_down_btn.clicked.connect(lambda: self._move_selected_queue(+1))
        self._queues_add_btn.clicked.connect(self._on_add_queue)
        self._queues_rename_btn.clicked.connect(self._on_rename_queue)
        self._queues_color_btn.clicked.connect(self._on_change_queue_color)
        self._queues_delete_btn.clicked.connect(self._on_delete_queue)
        for btn in (self._queues_up_btn, self._queues_down_btn, self._queues_add_btn, self._queues_rename_btn, self._queues_color_btn, self._queues_delete_btn):
            btn_row.addWidget(btn)
        btn_row.addStretch(1)
        queues_layout.addLayout(btn_row)

        layout.addWidget(queues_group)
        layout.addStretch()
        self._reload_queues()
        self._fit_queues_width_to_headers()
        return tab

    # -- Queue Manager methods --------------------------------------------------

    def _reload_queues(self):
        """Reload the queues table from the manager."""
        if not self._manager or not hasattr(self._manager, 'get_queues'):
            return
        try:
            queues = self._manager.get_queues()
            counts = self._manager._db.get_queue_download_counts()
        except (AttributeError, TypeError):
            return
        selected_id = self._selected_queue_id()

        self._queues_table.blockSignals(True)
        self._queues_table.setRowCount(0)
        for queue in queues:
            row = self._queues_table.rowCount()
            self._queues_table.insertRow(row)
            # Colour swatch + name in same cell
            name_item = QTableWidgetItem(queue.name)
            name_item.setData(Qt.ItemDataRole.UserRole, queue.id)
            if queue.is_default:
                name_item.setToolTip(
                    "The default queue. Every download starts here unless another queue "
                    "claims it. Its limits and colour are editable like any other."
                )
            self._queues_table.setItem(row, 0, name_item)
            self._queues_table.setCellWidget(row, 0, self._name_cell(queue))
            self._queues_table.setItem(
                row, 1, QTableWidgetItem(str(counts.get(queue.id, 0)))
            )
            # Editors for each limit column
            self._queues_table.setCellWidget(row, 2, self._limit_editor(queue))
            self._queues_table.setCellWidget(
                row, 3, self._bandwidth_editor(queue, upload=False)
            )
            self._queues_table.setCellWidget(
                row, 4, self._bandwidth_editor(queue, upload=True)
            )
        self._queues_table.blockSignals(False)

        self._refresh_queues_note()
        if selected_id:
            self._select_queue_id(selected_id)
        self._on_queues_selection_changed()

    def _name_cell(self, queue):
        """The swatch plus the queue name, as one cell."""
        holder = QWidget(self._queues_table)
        row = QHBoxLayout(holder)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(8)

        button = QPushButton(holder)
        button.setFixedSize(22, 22)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        colour = normalize_queue_color(queue.color) or DEFAULT_QUEUE_COLOR
        char = (queue.name or "").strip()[:1].upper()
        button.setText(char)
        qc = QColor(colour)
        luminance = (0.299 * qc.red() + 0.587 * qc.green() + 0.114 * qc.blue()) / 255.0
        text_color = "#000000" if luminance > 0.65 else "#ffffff"
        button.setStyleSheet(
            f"QPushButton {{ background-color: {colour}; color: {text_color}; "
            f"font-weight: bold; font-size: 11px; border: 1px solid #555; "
            f"border-radius: 4px; }}"
            f"QPushButton:hover {{ border: 2px solid #fff; }}"
        )
        button.setToolTip(f"Colour for '{queue.name}'. Click to change it.")
        button.clicked.connect(
            lambda _checked=False, qid=queue.id: self._on_pick_queue_color(qid)
        )
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)

        label = QLabel(queue.name, holder)
        label.setToolTip(
            f"'{queue.name}' - click the swatch to change its colour."
        )
        row.addWidget(label, 1)
        return holder

    def _limit_editor(self, queue):
        spin = QSpinBox(self._queues_table)
        spin.setRange(0, 99)
        spin.setValue(max(0, queue.max_concurrent))
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spin.setToolTip(
            "How many of this queue's downloads may run at once.\n"
            "0 = follow the global limit (no limit of its own)."
        )
        spin.valueChanged.connect(
            lambda value, qid=queue.id: self._on_queue_limit_changed(qid, value)
        )
        return spin

    def _bandwidth_editor(self, queue, upload: bool):
        stored = queue.upload_limit if upload else queue.download_limit
        spin = QSpinBox(self._queues_table)
        spin.setRange(0, 10_000_000)
        spin.setSingleStep(64)
        spin.setValue(max(0, int(stored or 0) // 1024))
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spin.setSuffix(" KB/s")
        direction = "upload" if upload else "download"
        spin.setToolTip(
            f"Ceiling on this queue's total {direction} rate, in KB/s.\n"
            "0 = follow the global limit (no limit of its own).\n"
            "Where both are set, the tighter one wins."
        )
        spin.valueChanged.connect(
            lambda value, qid=queue.id, up=upload: self._on_queue_bandwidth_changed(
                qid, up, value * 1024
            )
        )
        return spin

    def _on_pick_queue_color(self, queue_id: str):
        from PySide6.QtWidgets import QColorDialog

        queue = self._manager.get_queue(queue_id)
        if not queue:
            return
        current = QColor(normalize_queue_color(queue.color) or DEFAULT_QUEUE_COLOR)
        chosen = QColorDialog.getColor(current, self, f"Colour for '{queue.name}'")
        if not chosen.isValid():
            return
        ok, message = self._manager.set_queue_color(queue_id, chosen.name())
        if not ok and message:
            self._result_message = message
        self._reload_queues()

    def _on_change_queue_color(self):
        queue_id = self._selected_queue_id()
        if queue_id:
            self._on_pick_queue_color(queue_id)

    def _selected_queue_id(self) -> str:
        row = self._queues_table.currentRow()
        if row < 0:
            return ""
        item = self._queues_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _select_queue_id(self, queue_id: str):
        for row in range(self._queues_table.rowCount()):
            item = self._queues_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == queue_id:
                self._queues_table.selectRow(row)
                return

    def _on_queues_selection_changed(self):
        queue_id = self._selected_queue_id()
        queue = self._manager.get_queue(queue_id) if queue_id else None
        is_default = bool(queue and queue.is_default)
        self._queues_rename_btn.setEnabled(bool(queue) and not is_default)
        self._queues_color_btn.setEnabled(bool(queue))
        self._queues_delete_btn.setEnabled(bool(queue) and not is_default)
        rows = self._queues_table.rowCount()
        idx = self._queues_table.currentRow()
        movable = rows - 1 if rows else 0
        position = idx if idx > 0 else 0
        self._queues_up_btn.setEnabled(bool(queue) and not is_default and position > 0)
        self._queues_down_btn.setEnabled(bool(queue) and not is_default and position < movable - 1)

    def _on_queue_limit_changed(self, queue_id: str, value: int):
        self._manager.set_queue_max_concurrent(queue_id, value)
        self._refresh_queues_note()

    def _on_queue_bandwidth_changed(self, queue_id: str, upload: bool, value: int):
        queue = self._manager.get_queue(queue_id)
        if not queue:
            return
        download = value if not upload else queue.download_limit
        upload_limit = value if upload else queue.upload_limit
        self._manager.set_queue_limits(queue_id, download, upload_limit)
        self._refresh_queues_note()

    def _move_selected_queue(self, delta: int):
        queue_id = self._selected_queue_id()
        if not queue_id:
            return
        self._manager.move_queue_in_list(queue_id, delta)
        self._reload_queues()
        self._select_queue_id(queue_id)

    def _on_add_queue(self):
        dlg = AddQueueDialog(self, manager=self._manager)
        if not dlg.exec():
            return
        created, message = self._manager.create_queue(
            dlg.name.strip(), dlg.max_concurrent, dlg.color,
            dlg.download_limit_kb * 1024, dlg.upload_limit_kb * 1024,
        )
        self._result_message = message
        if created:
            self._reload_queues()
            new_q = next((q for q in self._manager.get_queues() if q.name.lower() == dlg.name.strip().lower()), None)
            if new_q:
                self._select_queue_id(new_q.id)

    def _on_rename_queue(self):
        queue_id = self._selected_queue_id()
        if not queue_id:
            return
        queue = self._manager.get_queue(queue_id)
        if not queue:
            return
        name, ok = QInputDialog.getText(
            self, "Rename Queue", "Queue name:", QLineEdit.Normal, queue.name
        )
        if not ok:
            return
        renamed, message = self._manager.rename_queue(queue_id, name.strip())
        self._result_message = message
        if renamed:
            self._reload_queues()
            self._select_queue_id(queue_id)

    def _on_delete_queue(self):
        queue_id = self._selected_queue_id()
        if not queue_id:
            return
        queue = self._manager.get_queue(queue_id)
        if not queue:
            return
        moved = len(self._manager._db.get_all_downloads(queue_id))
        if moved:
            text = (
                f"Delete '{queue.name}'?\n\n"
                f"{moved} download(s) will move to the Default queue. Downloads are never "
                "deleted with their queue."
            )
        else:
            text = f"Delete the empty queue '{queue.name}'?"
        if QMessageBox.question(self, "Delete Queue", text) != QMessageBox.StandardButton.Yes:
            return
        deleted, message = self._manager.delete_queue(queue_id)
        self._result_message = message
        if deleted:
            self._reload_queues()

    def _refresh_queues_note(self):
        """Explain how a queue limit interacts with the global limit, with the live values."""
        from my_idm.download_model import _format_speed

        # Guard against mock managers without full config objects
        if not self._manager or not hasattr(self._manager, 'general_config') or not hasattr(self._manager, 'network_config'):
            return
        try:
            general = self._manager.general_config
            net = self._manager.network_config
            global_max = general.effective_max_concurrent
            global_dl = net.download_limit or 0
            global_ul = net.upload_limit or 0
        except (AttributeError, TypeError):
            return
        # Additional guard: mock objects will have MagicMock attributes that fail comparison
        if not isinstance(global_dl, (int, float)) or not isinstance(global_ul, (int, float)) or not isinstance(global_max, (int, float)):
            return
        dl_text = f"{_format_speed(global_dl)} (unlimited)" if global_dl > 0 else "unlimited"
        ul_text = f"{_format_speed(global_ul)} (unlimited)" if global_ul > 0 else "unlimited"
        self._queues_note.setText(
            "A queue's limit caps how many of its own downloads run at once. "
            "Leave it at 0 for Global: the queue adds no cap of its own and follows the "
            f"global limit, currently {global_max} at a time "
            "(Tools → Preferences → General & Downloads).\n"
            "A download starts only when both its queue's limit and the global limit allow "
            "it, so a queue limit is a ceiling and never a reservation. Default holds every "
            "download that no other queue claims.\n"
            f"The bandwidth limits are in KB/s and follow the same rule: 0 follows the global "
            f"limit, currently {dl_text} down and {ul_text} up. Where both are set the tighter "
            "one wins, so a queue can slow its downloads down but never speed them up past the "
            "global limit. A download's own allocation (Low/Medium/High/Max) then takes its "
            "share of that."
        )

    def _header_column_widths(self):
        """Width each header needs, measured from this widget's own font."""
        header = self._queues_table.horizontalHeader()
        font = QFont(header.font())
        if font.pixelSize() > 0:
            font.setPixelSize(max(font.pixelSize(), 12))
        else:
            font.setPointSize(max(font.pointSize(), 10))
        font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(font)

        style = self._queues_table.style()
        padding = 2 * style.pixelMetric(QStyle.PixelMetric.PM_HeaderMargin) + 32

        widths = []
        headers = [
            "Queue",
            "Downloads",
            "Max at once\n(0 = Global)",
            "Download limit\n(0 = Global)",
            "Upload limit\n(0 = Global)",
        ]
        for column, text in enumerate(headers):
            widest = max(
                max(metrics.horizontalAdvance(line), metrics.horizontalAdvance(line.upper()))
                for line in text.split("\n")
            )
            min_widths = {
                1: 110,
                2: 120,
                3: 160,
                4: 160,
            }
            widths.append(max(widest + padding, min_widths.get(column, 0)))
        return widths

    def _fit_queues_width_to_headers(self):
        """Size the columns and the window to what the headers actually need."""
        widths = self._header_column_widths()
        for column, width in enumerate(widths):
            if column != 0:
                self._queues_table.setColumnWidth(column, width)

        margins = self.layout().contentsMargins()
        style = self._queues_table.style()
        chrome = (
            margins.left()
            + margins.right()
            + 2 * self._queues_table.frameWidth()
            + style.pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        )
        needed = sum(widths) + 170 + chrome

        MAX_DIALOG_WIDTH = 1400
        MIN_NAME_COLUMN_WIDTH = 170

        if needed > MAX_DIALOG_WIDTH:
            self._queues_table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.Interactive
            )
            self._queues_table.setColumnWidth(0, widths[0])
        else:
            self._queues_table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.Stretch
            )

        self.setMinimumWidth(min(needed, MAX_DIALOG_WIDTH))
        target = min(max(needed, self.width()), MAX_DIALOG_WIDTH)
        if target != self.width():
            self.resize(target, self.height())

    def _create_bandwidth_tab(self) -> QWidget:
        """Bandwidth Limit - configure daily/weekly/monthly bandwidth limits (global and per-queue)."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        bw_group = QGroupBox("Bandwidth Limits")
        bw_layout = QVBoxLayout(bw_group)
        bw_layout.setSpacing(12)
        bw_layout.setContentsMargins(14, 16, 14, 14)

        # 7 columns: Queue, Enabled, Limit, Limit Type, Progress, Percetage for Warning, Actions
        self._bw_table = QTableWidget(0, 7, self)
        self._bw_table.setHorizontalHeaderLabels([
            "Queue",
            "Enabled",
            "Limit",
            "Limit Type",
            "Progress",
            "Percetage for Warning",
            "Actions",
        ])
        _header = self._bw_table.horizontalHeader()
        _header.setFixedHeight(34)
        themed_widget(
            _header,
            """
            QHeaderView::section {
                background-color: {Colors.BG_MID};
                color: {Colors.TEXT_SECONDARY};
                border: none;
                border-bottom: 2px solid {Colors.BORDER};
                border-right: 1px solid {Colors.BORDER};
                padding: 4px 6px;
                font-weight: 600;
                font-size: 11px;
            }
            QHeaderView::section:hover {
                color: {Colors.TEXT};
                background-color: {Colors.BG_LIGHT};
            }
            """,
        )
        self._bw_table.verticalHeader().setVisible(False)
        self._bw_table.verticalHeader().setDefaultSectionSize(36)
        self._bw_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._bw_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._bw_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        _header.setStretchLastSection(False)
        _header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        _header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        _header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        _header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        _header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        _header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        _header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)

        self._bw_table.setColumnWidth(0, 150)
        self._bw_table.setColumnWidth(1, 80)
        self._bw_table.setColumnWidth(2, 90)
        self._bw_table.setColumnWidth(3, 105)
        self._bw_table.setColumnWidth(4, 210)
        self._bw_table.setColumnWidth(5, 175)
        self._bw_table.setColumnWidth(6, 85)
        bw_layout.addWidget(self._bw_table)

        btn_row = QHBoxLayout()
        self._bw_add_btn = QPushButton("➕ Add Limit…")
        self._bw_add_btn.clicked.connect(self._on_bw_add_limit)
        btn_row.addWidget(self._bw_add_btn)
        btn_row.addStretch(1)
        bw_layout.addLayout(btn_row)

        note_lbl = QLabel(
            "ℹ️ Global limits apply across all queues combined and override per-queue limits. "
            "When the warning percentage is reached, a warning badge appears at the top right of the menu bar. "
            "If 100% is reached, all active downloads and uploads are stopped automatically."
        )
        note_lbl.setWordWrap(True)
        note_lbl.setStyleSheet("color: #8fa0b5;")
        bw_layout.addWidget(note_lbl)

        layout.addWidget(bw_group, 1)
        layout.addStretch()

        self._reload_bandwidth_limits()
        return tab

    # -- Bandwidth Limit methods ----------------------------------------------

    def _reload_bandwidth_limits(self):
        """Reload the bandwidth limits table from the database."""
        db = self._db or (self._manager._db if self._manager else None)
        if not db:
            return

        try:
            limits = db.get_all_bandwidth_limits()
            queues = self._manager.get_queues() if self._manager and hasattr(self._manager, "get_queues") else db.get_queues()
            queue_names = {q.id: q.name for q in queues}
        except Exception as exc:
            log.warning("Failed to fetch bandwidth limits from database: %s", exc)
            return

        self._bw_table.blockSignals(True)
        try:
            self._bw_table.setRowCount(0)

            for limit in limits:
                row = self._bw_table.rowCount()
                self._bw_table.insertRow(row)

                qid = limit["queue_id"]
                if not qid or qid == "global":
                    q_name = "Global (All Queues)"
                else:
                    q_name = queue_names.get(qid, qid)

                # 0. Queue
                item_q = QTableWidgetItem(q_name)
                item_q.setData(Qt.ItemDataRole.UserRole, limit["id"])
                if not qid or qid == "global":
                    font = item_q.font()
                    font.setBold(True)
                    item_q.setFont(font)
                self._bw_table.setItem(row, 0, item_q)

                # 1. Enabled
                cb_container = QWidget()
                cb_layout = QHBoxLayout(cb_container)
                cb_layout.setContentsMargins(0, 0, 0, 0)
                cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cb = QCheckBox()
                cb.setChecked(bool(limit["enabled"]))
                cb.toggled.connect(lambda checked, l_id=limit["id"]: self._on_bw_toggle_enabled(l_id, checked))
                cb_layout.addWidget(cb)
                self._bw_table.setCellWidget(row, 1, cb_container)

                # 2. Limit
                lim_bytes = limit["limit_bytes"]
                lim_text = humanize.naturalsize(lim_bytes, binary=True) if lim_bytes > 0 else "Unlimited"
                item_lim = QTableWidgetItem(lim_text)
                item_lim.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._bw_table.setItem(row, 2, item_lim)

                # 3. Limit Type
                pt = limit["limit_type"].capitalize()
                item_pt = QTableWidgetItem(pt)
                item_pt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._bw_table.setItem(row, 3, item_pt)

                # 4. Progress
                dl, ul = db.get_current_period_usage(qid, limit["limit_type"])
                used = dl + ul
                pct = (used / lim_bytes * 100) if lim_bytes > 0 else 0.0
                prog_text = f"{humanize.naturalsize(used, binary=True)} / {lim_text} ({pct:.1f}%)"
                item_prog = QTableWidgetItem(prog_text)
                item_prog.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if pct >= 100.0:
                    item_prog.setForeground(QColor("#ef4444"))
                elif pct >= limit["warning_percent"]:
                    item_prog.setForeground(QColor("#f59e0b"))
                self._bw_table.setItem(row, 4, item_prog)

                # 5. Percetage for Warning
                warn_text = f"{limit['warning_percent']}%"
                item_warn = QTableWidgetItem(warn_text)
                item_warn.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._bw_table.setItem(row, 5, item_warn)

                # 6. Actions (Edit & Delete buttons with QIcons)
                actions_widget = QWidget()
                actions_layout = QHBoxLayout(actions_widget)
                actions_layout.setContentsMargins(4, 0, 4, 0)
                actions_layout.setSpacing(6)
                actions_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

                btn_edit = QPushButton()
                btn_edit.setIcon(_create_action_icon("✏️", 18))
                btn_edit.setIconSize(QSize(16, 16))
                btn_edit.setFixedSize(30, 26)
                btn_edit.setToolTip("Edit bandwidth limit")
                btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
                btn_edit.clicked.connect(lambda checked, l=limit: self._on_bw_edit_limit(l))
                actions_layout.addWidget(btn_edit)

                btn_del = QPushButton()
                btn_del.setIcon(_create_action_icon("🗑️", 18))
                btn_del.setIconSize(QSize(16, 16))
                btn_del.setFixedSize(30, 26)
                btn_del.setToolTip("Delete bandwidth limit")
                btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
                btn_del.clicked.connect(lambda checked, l_id=limit["id"]: self._on_bw_delete_limit(l_id))
                actions_layout.addWidget(btn_del)

                self._bw_table.setCellWidget(row, 6, actions_widget)
        except Exception as exc:
            log.warning("Error rendering bandwidth limits table: %s", exc)
        finally:
            self._bw_table.blockSignals(False)

    def _on_bw_toggle_enabled(self, limit_id: int, enabled: bool):
        db = self._db or (self._manager._db if self._manager else None)
        if not db:
            return
        limits = db.get_all_bandwidth_limits()
        limit = next((l for l in limits if l["id"] == limit_id), None)
        if limit:
            db.update_bandwidth_limit(
                limit_id, enabled, limit["limit_bytes"],
                limit["limit_type"], limit["warning_percent"]
            )
            if self._manager and hasattr(self._manager, "check_all_bandwidth_limits"):
                self._manager.check_all_bandwidth_limits()
            self._reload_bandwidth_limits()

    def _on_bw_add_limit(self):
        """Add a new bandwidth limit."""
        db = self._db or (self._manager._db if self._manager else None)
        dlg = BandwidthLimitDialog(self, manager=self._manager, db=db)
        if dlg.exec():
            if db:
                db.create_bandwidth_limit(
                    dlg.queue_id, dlg.enabled, dlg.limit_bytes,
                    dlg.limit_type, dlg.warning_percent
                )
            if self._manager and hasattr(self._manager, "check_all_bandwidth_limits"):
                self._manager.check_all_bandwidth_limits()
            self._reload_bandwidth_limits()

    def _on_bw_edit_limit(self, limit: dict):
        """Edit an existing bandwidth limit."""
        db = self._db or (self._manager._db if self._manager else None)
        dlg = BandwidthLimitDialog(self, manager=self._manager, db=db, limit=limit)
        if dlg.exec():
            if db:
                db.update_bandwidth_limit(
                    limit["id"], dlg.enabled, dlg.limit_bytes,
                    dlg.limit_type, dlg.warning_percent
                )
            if self._manager and hasattr(self._manager, "check_all_bandwidth_limits"):
                self._manager.check_all_bandwidth_limits()
            self._reload_bandwidth_limits()

    def _on_bw_delete_limit(self, limit_id: int):
        """Delete selected bandwidth limit."""
        if QMessageBox.question(self, "Delete Limit", "Delete this bandwidth limit?") != QMessageBox.StandardButton.Yes:
            return

        db = self._db or (self._manager._db if self._manager else None)
        if db:
            db.delete_bandwidth_limit(limit_id)
            if self._manager and hasattr(self._manager, "check_all_bandwidth_limits"):
                self._manager.check_all_bandwidth_limits()
            self._reload_bandwidth_limits()

    def _create_scheduler_tab(self) -> QWidget:
        """Download scheduler (off-peak hours) settings tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        sched_group = QGroupBox("Off-Peak Download Scheduler")
        sched_layout = QVBoxLayout(sched_group)
        sched_layout.setSpacing(12)
        sched_layout.setContentsMargins(14, 16, 14, 14)

        self._scheduler_enable_cb = QCheckBox("Enable off-peak download scheduler")
        self._scheduler_enable_cb.setToolTip(
            "When enabled, queued downloads only start during specified off-peak hours unless force-started."
        )
        self._scheduler_enable_cb.toggled.connect(self._on_scheduler_enable_toggled)
        sched_layout.addWidget(self._scheduler_enable_cb)

        self._scheduler_controls_widget = QWidget()
        ctrls_layout = QVBoxLayout(self._scheduler_controls_widget)
        ctrls_layout.setContentsMargins(0, 4, 0, 0)
        ctrls_layout.setSpacing(12)

        # Time window row
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Off-peak start time:"))
        self._scheduler_start_time = QTimeEdit()
        self._scheduler_start_time.setDisplayFormat("HH:mm")
        self._scheduler_start_time.setToolTip("Start time of off-peak window (local time)")
        time_row.addWidget(self._scheduler_start_time)

        time_row.addSpacing(16)
        time_row.addWidget(QLabel("Off-peak end time:"))
        self._scheduler_end_time = QTimeEdit()
        self._scheduler_end_time.setDisplayFormat("HH:mm")
        self._scheduler_end_time.setToolTip("End time of off-peak window (local time)")
        time_row.addWidget(self._scheduler_end_time)
        time_row.addStretch(1)
        ctrls_layout.addLayout(time_row)

        time_hint = QLabel(
            "ℹ️ Times use your local system clock. Overnight windows spanning midnight "
            "(e.g., 23:00 to 07:00) are fully supported."
        )
        time_hint.setWordWrap(True)
        time_hint.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        ctrls_layout.addWidget(time_hint)

        # Pause toggle
        self._scheduler_pause_cb = QCheckBox("Pause active downloads when off-peak window ends")
        self._scheduler_pause_cb.setToolTip(
            "Automatically pause downloading items when leaving off-peak hours. "
            "Force-started downloads will continue running."
        )
        ctrls_layout.addWidget(self._scheduler_pause_cb)

        # Active days of week
        days_group = QGroupBox("Active Days")
        days_layout = QVBoxLayout(days_group)
        days_layout.setSpacing(8)

        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        self._scheduler_day_cbs: list[QCheckBox] = []
        days_row = QHBoxLayout()
        for d in day_names:
            cb = QCheckBox(d[:3])
            cb.setToolTip(f"Active on {d}")
            self._scheduler_day_cbs.append(cb)
            days_row.addWidget(cb)
        days_row.addStretch(1)
        days_layout.addLayout(days_row)

        days_btn_row = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: [cb.setChecked(True) for cb in self._scheduler_day_cbs])
        select_none_btn = QPushButton("Clear All")
        select_none_btn.clicked.connect(lambda: [cb.setChecked(False) for cb in self._scheduler_day_cbs])
        days_btn_row.addWidget(select_all_btn)
        days_btn_row.addWidget(select_none_btn)
        days_btn_row.addStretch(1)
        days_layout.addLayout(days_btn_row)

        ctrls_layout.addWidget(days_group)
        sched_layout.addWidget(self._scheduler_controls_widget)
        layout.addWidget(sched_group)

        # Force Start override note
        override_group = QGroupBox("Force Start Override")
        ov_layout = QVBoxLayout(override_group)
        ov_lbl = QLabel(
            "⚡ <b>Force Start:</b> You can override the off-peak schedule at any time for specific downloads.<br>"
            "Click the <b>Force Start</b> button on the toolbar (or select it from the Edit / Context menu).<br>"
            "Force-started downloads immediately bypass the off-peak schedule and will not be paused when the window ends."
        )
        ov_lbl.setTextFormat(Qt.TextFormat.RichText)
        ov_lbl.setWordWrap(True)
        ov_layout.addWidget(ov_lbl)
        layout.addWidget(override_group)

        layout.addStretch(1)
        return tab

    def _on_scheduler_enable_toggled(self, checked: bool):
        if hasattr(self, "_scheduler_controls_widget"):
            self._scheduler_controls_widget.setEnabled(checked)


    def _create_youtube_tab(self) -> QWidget:
        """YouTube / yt-dlp settings, split out of the combined External Tools tab.

        ``ExternalToolsConfig`` configures exactly two tools - the AnimePahe scraper and
        yt-dlp - so one tab per tool is the honest split. The AnimePahe tab keeps its own
        nested "Download Anime by URL" and "Diagnostics & Logs" groups, which are
        actions on that same tool.
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.addWidget(self._create_youtube_group())
        layout.addStretch()
        return tab

    def _create_youtube_group(self) -> QGroupBox:
        """YouTube / yt-dlp configuration (Phase 6 of the YouTube scraper)."""
        group = QGroupBox("YouTube Downloader (yt-dlp)")
        gl = QVBoxLayout(group)
        gl.setSpacing(10)

        cfg = self._external_tools_cfg

        self._yt_enabled_cb = QCheckBox("Enable YouTube integration")
        self._yt_enabled_cb.setToolTip(
            "When disabled, pasting a YouTube link in the Add Download dialog\n"
            "will not offer the YouTube downloader."
        )
        self._yt_enabled_cb.setChecked(cfg.ytdlp_enabled)
        self._yt_enabled_cb.toggled.connect(self._on_youtube_enabled_toggled)
        gl.addWidget(self._yt_enabled_cb)

        # -- tool locations --
        tools_group = QGroupBox("Tool Locations")
        tg = QGridLayout(tools_group)

        self._yt_path_edit = QLineEdit()
        self._yt_path_edit.setPlaceholderText("auto-detect (pip-installed yt-dlp or on PATH)")
        self._yt_path_edit.setText(cfg.ytdlp_path)
        self._yt_path_edit.editingFinished.connect(self._on_youtube_paths_changed)
        tg.addWidget(QLabel("yt-dlp:"), 0, 0)
        tg.addWidget(self._yt_path_edit, 0, 1)
        yt_browse = QPushButton("Browse…")
        yt_browse.clicked.connect(self._on_browse_ytdlp)
        tg.addWidget(yt_browse, 0, 2)
        self._yt_path_status = QLabel("Checking\u2026")
        tg.addWidget(self._yt_path_status, 0, 3)

        self._yt_ffmpeg_edit = QLineEdit()
        self._yt_ffmpeg_edit.setPlaceholderText("auto-detect (ffmpeg on PATH)")
        self._yt_ffmpeg_edit.setText(cfg.ytdlp_ffmpeg_path)
        self._yt_ffmpeg_edit.editingFinished.connect(self._on_youtube_paths_changed)
        tg.addWidget(QLabel("ffmpeg:"), 1, 0)
        tg.addWidget(self._yt_ffmpeg_edit, 1, 1)
        ff_browse = QPushButton("Browse…")
        ff_browse.clicked.connect(self._on_browse_ffmpeg)
        tg.addWidget(ff_browse, 1, 2)
        self._yt_ffmpeg_status = QLabel("Checking\u2026")
        tg.addWidget(self._yt_ffmpeg_status, 1, 3)

        gl.addWidget(tools_group)

        # -- version / update --
        ver_row = QHBoxLayout()
        self._yt_version_lbl = QLabel("Checking\u2026")
        self._yt_version_lbl.setStyleSheet("color: #8fa0b5;")
        ver_row.addWidget(self._yt_version_lbl, 1)
        self._yt_update_btn = QPushButton("Update yt-dlp")
        self._yt_update_btn.setToolTip(
            "Runs 'pip install -U yt-dlp', or 'yt-dlp -U' when only the\n"
            "standalone binary is available."
        )
        self._yt_update_btn.clicked.connect(self._on_update_ytdlp)
        ver_row.addWidget(self._yt_update_btn)
        gl.addLayout(ver_row)

        # -- default format --
        fmt_group = QGroupBox("Default Quality")
        fg = QVBoxLayout(fmt_group)
        self._yt_format_combo = QComboBox()
        from my_idm.youtube_dialog import QUALITY_PRESETS
        for label, selector, _h in QUALITY_PRESETS:
            self._yt_format_combo.addItem(label, selector)
        current = cfg.ytdlp_default_format
        index = self._yt_format_combo.findData(current)
        if index >= 0:
            self._yt_format_combo.setCurrentIndex(index)
        else:
            self._yt_format_combo.addItem(f"Custom: {current}", current)
            self._yt_format_combo.setCurrentIndex(self._yt_format_combo.count() - 1)
        self._yt_format_combo.setToolTip(
            "yt-dlp format selector used by default for YouTube downloads.\n"
            "Merged (video+audio) selections require ffmpeg."
        )
        fg.addWidget(self._yt_format_combo)

        self._yt_prefer_mode_a_cb = QCheckBox("Prefer direct URL mode when a single stream is available")
        self._yt_prefer_mode_a_cb.setToolTip(
            "Mode A hands the CDN URL to My-IDM for segmented, resumable downloading.\n"
            "Only possible for audio-only or combined single-file formats — YouTube\n"
            "rarely offers combined video+audio streams, so video uses Mode B."
        )
        self._yt_prefer_mode_a_cb.setChecked(cfg.ytdlp_prefer_mode_a)
        fg.addWidget(self._yt_prefer_mode_a_cb)
        gl.addWidget(fmt_group)

        # -- post-processing --
        post_group = QGroupBox("Post-processing (yt-dlp downloads only)")
        pg = QVBoxLayout(post_group)
        self._yt_embed_thumb_cb = QCheckBox("Embed thumbnail in the downloaded file")
        self._yt_embed_thumb_cb.setChecked(cfg.ytdlp_embed_thumbnail)
        self._yt_embed_thumb_cb.setToolTip("Requires ffmpeg.")
        pg.addWidget(self._yt_embed_thumb_cb)

        subs_row = QHBoxLayout()
        self._yt_embed_subs_cb = QCheckBox("Download and embed subtitles")
        self._yt_embed_subs_cb.setChecked(cfg.ytdlp_embed_subtitles)
        subs_row.addWidget(self._yt_embed_subs_cb)
        self._yt_subs_langs_edit = QLineEdit(cfg.ytdlp_subtitle_langs)
        self._yt_subs_langs_edit.setPlaceholderText("en, ja")
        self._yt_subs_langs_edit.setMaximumWidth(200)
        self._yt_subs_langs_edit.setEnabled(cfg.ytdlp_embed_subtitles)
        self._yt_embed_subs_cb.toggled.connect(self._yt_subs_langs_edit.setEnabled)
        subs_row.addWidget(QLabel("Languages:"))
        subs_row.addWidget(self._yt_subs_langs_edit)
        subs_row.addStretch()
        pg.addLayout(subs_row)
        gl.addWidget(post_group)

        # -- authentication --
        auth_group = QGroupBox("Authentication")
        ag = QVBoxLayout(auth_group)
        cookie_row = QHBoxLayout()
        cookie_row.addWidget(QLabel("Cookie source:"))
        self._yt_cookies_combo = QComboBox()
        from my_idm.youtube_tool import SUPPORTED_BROWSERS
        self._yt_cookies_combo.addItem("None", "")
        for name in SUPPORTED_BROWSERS:
            self._yt_cookies_combo.addItem(name.capitalize(), name)
        existing = cfg.ytdlp_cookies_browser
        idx = self._yt_cookies_combo.findData(existing)
        if idx >= 0:
            self._yt_cookies_combo.setCurrentIndex(idx)
        cookie_row.addWidget(self._yt_cookies_combo, 1)
        ag.addLayout(cookie_row)

        warn = QLabel(
            "⚠️ Browser cookies expose your account credentials to yt-dlp and to any\n"
            "site it visits. Use a throwaway account. Private and age-restricted\n"
            "videos cannot be downloaded without a cookie source."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #f59e0b; font-size: 11px;")
        ag.addWidget(warn)
        gl.addWidget(auth_group)

        # -- misc --
        misc_group = QGroupBox("Misc")
        mg = QVBoxLayout(misc_group)
        self._yt_autodetect_cb = QCheckBox(
            "Auto-detect YouTube URLs in the Add Download dialog"
        )
        self._yt_autodetect_cb.setChecked(cfg.ytdlp_auto_detect_urls)
        mg.addWidget(self._yt_autodetect_cb)

        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("Playlist entries to list:"))
        self._yt_playlist_limit_spin = QSpinBox()
        self._yt_playlist_limit_spin.setRange(1, 500)
        self._yt_playlist_limit_spin.setValue(
            clamp_ytdlp_playlist_limit(cfg.ytdlp_playlist_limit)
        )
        self._yt_playlist_limit_spin.setToolTip(
            "Caps how many playlist or channel entries are listed per analysis.\n"
            "Each analysis makes at most two requests regardless of this value, so\n"
            "a large playlist cannot flood the site. Raise it only if you need more."
        )
        limit_row.addWidget(self._yt_playlist_limit_spin)
        limit_row.addStretch()
        mg.addLayout(limit_row)
        gl.addWidget(misc_group)
        args_row = QHBoxLayout()
        args_row.addWidget(QLabel("Extra yt-dlp args:"))
        self._yt_extra_args_edit = QLineEdit(cfg.ytdlp_extra_args)
        self._yt_extra_args_edit.setPlaceholderText("--retries 5 --concurrent-fragments 4")
        args_row.addWidget(self._yt_extra_args_edit, 1)
        mg.addLayout(args_row)
        gl.addWidget(misc_group)

        # Only the enable/disable wiring runs here. Probing for yt-dlp costs a `import yt_dlp` plus
        # a subprocess, and doing it during construction blocked the dialog's first paint for the
        # best part of a second. `_on_youtube_enabled_toggled` used to refresh, and the line after
        # it refreshed again, so the work was done twice before the dialog was even on screen.
        # The status labels start on "Checking…" and are filled in by `_apply_probe_results`.
        self._on_youtube_enabled_toggled(cfg.ytdlp_enabled, probe=False)
        return group

    # -- YouTube settings handlers ------------------------------------------

    def _on_youtube_enabled_toggled(self, enabled: bool, probe: bool = True):
        for widget in (
            self._yt_path_edit, self._yt_ffmpeg_edit, self._yt_format_combo,
            self._yt_prefer_mode_a_cb, self._yt_embed_thumb_cb, self._yt_embed_subs_cb,
            self._yt_cookies_combo, self._yt_autodetect_cb, self._yt_extra_args_edit,
            self._yt_playlist_limit_spin, self._yt_update_btn,
        ):
            widget.setEnabled(enabled)
        self._yt_subs_langs_edit.setEnabled(enabled and self._yt_embed_subs_cb.isChecked())
        if enabled and probe:
            self._refresh_youtube_status()

    def _current_youtube_config(self) -> ExternalToolsConfig:
        """Build a config snapshot from the current widget values."""
        cfg = self._external_tools_cfg
        cfg.ytdlp_enabled = self._yt_enabled_cb.isChecked()
        cfg.ytdlp_path = self._yt_path_edit.text().strip()
        cfg.ytdlp_ffmpeg_path = self._yt_ffmpeg_edit.text().strip()
        cfg.ytdlp_default_format = self._yt_format_combo.currentData() or cfg.ytdlp_default_format
        cfg.ytdlp_prefer_mode_a = self._yt_prefer_mode_a_cb.isChecked()
        cfg.ytdlp_embed_thumbnail = self._yt_embed_thumb_cb.isChecked()
        cfg.ytdlp_embed_subtitles = self._yt_embed_subs_cb.isChecked()
        cfg.ytdlp_subtitle_langs = self._yt_subs_langs_edit.text().strip() or "en"
        cfg.ytdlp_cookies_browser = self._yt_cookies_combo.currentData() or ""
        cfg.ytdlp_auto_detect_urls = self._yt_autodetect_cb.isChecked()
        cfg.ytdlp_playlist_limit = self._yt_playlist_limit_spin.value()
        cfg.ytdlp_extra_args = self._yt_extra_args_edit.text().strip()
        return cfg

    def _refresh_youtube_status(self):
        """Live validation of the configured yt-dlp and ffmpeg paths.

        Queries the version once and reuses it: this used to ask twice per call, and the caller
        itself could run twice per dialog open, so a single visible refresh cost up to four
        `yt-dlp --version` subprocesses.
        """
        from my_idm import youtube_tool as ytt

        cfg = self._current_youtube_config()

        ytdlp_path = cfg.get_effective_ytdlp_path()
        version = ytt.get_ytdlp_version(cfg) if ytdlp_path else ""
        if ytdlp_path:
            self._yt_path_status.setText(f"✓ {version}" if version else "✓ found")
            self._yt_path_status.setStyleSheet("color: #3fb950;")
        else:
            self._yt_path_status.setText("✗ not found — pip install -U yt-dlp")
            self._yt_path_status.setStyleSheet("color: #f85149;")

        if cfg.get_effective_ffmpeg_path():
            self._yt_ffmpeg_status.setText("✓ found")
            self._yt_ffmpeg_status.setStyleSheet("color: #3fb950;")
        else:
            self._yt_ffmpeg_status.setText("✗ not found")
            self._yt_ffmpeg_status.setStyleSheet("color: #f85149;")

        if not ytdlp_path:
            self._yt_version_lbl.setText("yt-dlp unavailable")
        else:
            self._yt_version_lbl.setText(f"yt-dlp {version or 'unknown'}")

    def _on_youtube_paths_changed(self):
        self._refresh_youtube_status()

    def _on_browse_ytdlp(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select yt-dlp executable", self._yt_path_edit.text() or "",
            "Executables (*.exe);;All Files (*)",
        )
        if path:
            self._yt_path_edit.setText(normalize_path(path))
            self._refresh_youtube_status()

    def _on_browse_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ffmpeg executable", self._yt_ffmpeg_edit.text() or "",
            "Executables (*.exe);;All Files (*)",
        )
        if path:
            self._yt_ffmpeg_edit.setText(normalize_path(path))
            self._refresh_youtube_status()

    def _on_update_ytdlp(self):
        from my_idm import youtube_tool as ytt

        cfg = self._current_youtube_config()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, message = ytt.update_ytdlp(cfg)
        finally:
            QApplication.restoreOverrideCursor()

        if ok:
            QMessageBox.information(self, "yt-dlp Updated", message or "yt-dlp is up to date.")
        else:
            QMessageBox.warning(self, "yt-dlp Update Failed", message)
        self._refresh_youtube_status()

    def _create_browser_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setContentsMargins(14, 16, 14, 14)

        # 1. Server Configuration Group
        server_group = QGroupBox("Local Extension Loopback Server")
        server_layout = QVBoxLayout(server_group)
        server_layout.setSpacing(10)

        self._browser_enabled_cb = QCheckBox("Enable Browser Integration (starts HTTP loopback listener)")
        self._browser_enabled_cb.setToolTip("Enables the local REST server that receives downloads from the Chrome extension.")
        server_layout.addWidget(self._browser_enabled_cb)

        port_row = QHBoxLayout()
        port_lbl = QLabel("Server Port:")
        port_row.addWidget(port_lbl)

        self._browser_port_spin = QSpinBox()
        self._browser_port_spin.setRange(1024, 65535)
        self._browser_port_spin.setValue(self._browser_cfg.port or 19582)
        port_row.addWidget(self._browser_port_spin)

        self._browser_status_lbl = QLabel("Checking status...")
        self._browser_status_lbl.setStyleSheet("color: #8fa0b5; margin-left: 12px;")
        port_row.addWidget(self._browser_status_lbl, 1)
        server_layout.addLayout(port_row)

        self._browser_intercept_cb = QCheckBox("Take downloads from the browser")
        self._browser_intercept_cb.setToolTip(
            "When off, My-IDM declines every capture from the browser: automatic interception "
            "is cancelled and a right-click capture is declined too. Same switch as the "
            "🎯 Download Capture tray item and the global hotkey."
        )
        server_layout.addWidget(self._browser_intercept_cb)

        self._browser_intercept_torrent_cb = QCheckBox("Intercept .torrent files from browser")
        self._browser_intercept_torrent_cb.setToolTip("When enabled, .torrent file downloads are intercepted and added as torrents in My-IDM.")
        self._browser_intercept_torrent_cb.setChecked(self._browser_cfg.intercept_torrent_files)
        server_layout.addWidget(self._browser_intercept_torrent_cb)

        self._browser_intercept_magnet_cb = QCheckBox("Intercept magnet links from browser")
        self._browser_intercept_magnet_cb.setToolTip("When enabled, magnet link clicks are intercepted and added as torrents in My-IDM.")
        self._browser_intercept_magnet_cb.setChecked(self._browser_cfg.intercept_magnet_links)
        server_layout.addWidget(self._browser_intercept_magnet_cb)

        min_size_row = QHBoxLayout()
        min_size_lbl = QLabel("Minimum file size to intercept (KB, 0 = no limit):")
        min_size_row.addWidget(min_size_lbl)

        self._browser_min_size_spin = QSpinBox()
        self._browser_min_size_spin.setRange(0, 1000000)
        self._browser_min_size_spin.setValue(self._browser_cfg.min_file_size_kb)
        self._browser_min_size_spin.setToolTip("Downloads smaller than this size (in KB) will not be intercepted by My-IDM. Set to 0 to disable.")
        min_size_row.addWidget(self._browser_min_size_spin)
        min_size_row.addStretch()
        server_layout.addLayout(min_size_row)

        self._browser_skip_unknown_size_cb = QCheckBox(
            "Skip downloads whose size cannot be determined"
        )
        self._browser_skip_unknown_size_cb.setToolTip(
            "Only applies while a minimum size is set above.\n\n"
            "The browser usually cannot report a size at the moment a download starts, and a "
            "chunked or dynamically generated response has no Content-Length to look for either.\n\n"
            "Checked: such a download is left to the browser, because a minimum you configured is "
            "a statement about what you want to see in My-IDM.\n"
            "Unchecked: it is captured anyway, and My-IDM checks the real size against the minimum "
            "once it has probed the response - so it can still be dropped, but only after it "
            "appears."
        )
        self._browser_skip_unknown_size_cb.setChecked(
            self._browser_cfg.skip_unknown_size_downloads
        )
        self._browser_skip_unknown_size_cb.setEnabled(self._browser_cfg.min_file_size_kb > 0)
        server_layout.addWidget(self._browser_skip_unknown_size_cb)
        self._browser_min_size_spin.valueChanged.connect(
            self._on_browser_min_size_changed
        )

        bypass_lbl = QLabel("Bypassed File Extensions (comma-separated):")
        server_layout.addWidget(bypass_lbl)

        self._browser_bypass_edit = QLineEdit()
        self._browser_bypass_edit.setPlaceholderText(".torrent, .crx, .pdf")
        self._browser_bypass_edit.setText(", ".join(self._browser_cfg.bypassed_extensions))
        server_layout.addWidget(self._browser_bypass_edit)

        layout.addWidget(server_group)

        # 2. Chromium Browsers Setup Group
        chrome_group = QGroupBox("Chromium Browsers (Chrome / Brave / Edge / Opera)")
        self._chrome_group = chrome_group
        chrome_layout = QVBoxLayout(chrome_group)
        chrome_layout.setSpacing(8)

        chrome_instructions = (
            "<p style='line-height: 1.6; margin: 0;'>"
            "1. Navigate to: <a href='chrome://extensions/' style='color: #00d2ff; text-decoration: underline; font-weight: bold;'>chrome://extensions/</a> "
            "(or <a href='edge://extensions/' style='color: #00d2ff; text-decoration: underline; font-weight: bold;'>edge://extensions/</a>) "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy URL]</span><br>"
            "2. Turn <b>ON</b> <b>Developer mode</b> (toggle switch in the top right corner).<br>"
            "3. Click <b>Load unpacked</b> and select the <a href='copy:extension_path' style='color: #00d2ff; text-decoration: underline; font-weight: bold;'>browser_extension</a> folder "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy folder path]</span>"
            "</p>"
        )
        self._chrome_instr_lbl = QLabel(chrome_instructions)
        self._chrome_instr_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._chrome_instr_lbl.setWordWrap(True)
        self._chrome_instr_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self._chrome_instr_lbl.linkActivated.connect(self._on_browser_url_clicked)
        chrome_layout.addWidget(self._chrome_instr_lbl)
        self._instr_lbl = self._chrome_instr_lbl  # backward compatibility

        chrome_action_row = QHBoxLayout()
        self._btn_open_ext_folder = QPushButton("📁 Open Extension Folder")
        self._btn_open_ext_folder.setToolTip("Open the browser_extension folder in Windows File Explorer")
        self._btn_open_ext_folder.clicked.connect(self._on_open_extension_folder)
        chrome_action_row.addWidget(self._btn_open_ext_folder)

        self._chrome_copy_lbl = QLabel("")
        self._chrome_copy_lbl.setStyleSheet("color: #2ed573; font-weight: bold; margin-left: 8px;")
        chrome_action_row.addWidget(self._chrome_copy_lbl, 1)

        chrome_layout.addLayout(chrome_action_row)
        layout.addWidget(chrome_group)

        # 3. Mozilla Firefox Setup Group
        firefox_group = QGroupBox("Mozilla Firefox")
        firefox_layout = QVBoxLayout(firefox_group)
        firefox_layout.setSpacing(8)

        firefox_instructions = (
            "<p style='line-height: 1.6; margin: 0;'>"
            "1. <i>Session Only (Temporary):</i> Navigate to <a href='about:debugging#/runtime/this-firefox' style='color: #ff9d00; text-decoration: underline; font-weight: bold;'>about:debugging#/runtime/this-firefox</a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy URL]</span>, click <b>Load Temporary Add-on...</b>, and select <a href='copy:manifest_path' style='color: #ff9d00; text-decoration: underline; font-weight: bold;'>manifest.json</a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy manifest path]</span>.<br>"
            "<span style='color: #ffb86c;'><i>⚠️ Note: Firefox removes temporary extensions on browser restart by design.</i></span><br>"
            "2. <i>Permanent Installation (Retained Across Restarts):</i> In Developer/ESR/Nightly/Floorp, toggle <a href='xpinstall.signatures.required' style='color: #ff9d00; text-decoration: underline; font-weight: bold;'>xpinstall.signatures.required</a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy]</span> to <b>false</b> in <a href='about:config' style='color: #ff9d00; text-decoration: underline; font-weight: bold;'>about:config</a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy URL]</span>, click <b>📦 Package Firefox Add-on (.xpi)</b> below, and install via <a href='about:addons' style='color: #ff9d00; text-decoration: underline; font-weight: bold;'>about:addons</a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy URL]</span> (or view <b>🦊 Permanent Setup Guide</b> for free AMO signing)."
            "</p>"
        )
        self._firefox_instr_lbl = QLabel(firefox_instructions)
        self._firefox_instr_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._firefox_instr_lbl.setWordWrap(True)
        self._firefox_instr_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self._firefox_instr_lbl.linkActivated.connect(self._on_browser_url_clicked)
        firefox_layout.addWidget(self._firefox_instr_lbl)

        firefox_action_row = QHBoxLayout()
        self._btn_package_firefox = QPushButton("📦 Package Firefox Add-on (.xpi)")
        self._btn_package_firefox.setToolTip("Package browser extension into a standard .xpi archive for Firefox")
        self._btn_package_firefox.clicked.connect(self._on_package_firefox_extension)
        firefox_action_row.addWidget(self._btn_package_firefox)

        self._btn_firefox_guide = QPushButton("🦊 Permanent Firefox Guide")
        self._btn_firefox_guide.setToolTip("Step-by-step guide to retaining Firefox extension across browser restarts")
        self._btn_firefox_guide.clicked.connect(self._on_open_firefox_guide)
        firefox_action_row.addWidget(self._btn_firefox_guide)

        self._firefox_copy_lbl = QLabel("")
        self._firefox_copy_lbl.setStyleSheet("color: #2ed573; font-weight: bold; margin-left: 8px;")
        firefox_action_row.addWidget(self._firefox_copy_lbl, 1)

        firefox_layout.addLayout(firefox_action_row)
        layout.addWidget(firefox_group)

        layout.addStretch()
        return tab

    def _get_extension_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "browser_extension"

    def _on_open_extension_folder(self):
        ext_dir = self._get_extension_dir()
        if ext_dir.is_dir():
            ok, msg = show_in_folder(ext_dir)
            if not ok:
                QMessageBox.warning(self, "Folder Error", msg)
        else:
            QMessageBox.warning(self, "Folder Not Found", f"Extension folder does not exist:\n{ext_dir}")

    def _on_browser_url_clicked(self, url: str):
        from PySide6.QtWidgets import QApplication, QToolTip
        from PySide6.QtGui import QClipboard, QCursor
        from PySide6.QtCore import QTimer

        target = url
        if target.startswith("copy:"):
            text = target[len("copy:"):]
        else:
            text = target

        if text == "extension_path":
            text = str(self._get_extension_dir())
        elif text == "manifest_path":
            text = str(self._get_extension_dir() / "manifest.json")
        elif text == "xpi_path":
            text = str(self._get_extension_dir() / "my-idm-firefox.xpi")

        if text.startswith("http://") or text.startswith("https://"):
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(text))
            return

        cb = QApplication.clipboard()
        if cb:
            for _ in range(5):
                cb.setText(text)
                if cb.text() == text:
                    break
                import time
                time.sleep(0.015)
        QApplication.processEvents()
        try:
            from PySide6.QtCore import QRect
            QToolTip.showText(QCursor.pos(), f"✓ Copied: {text}", self, QRect(), 2500)
        except Exception:
            pass

        feedback = f"✓ Copied '{text}' to clipboard"
        if hasattr(self, "_chrome_copy_lbl"):
            self._chrome_copy_lbl.setText(feedback)
            QTimer.singleShot(3500, lambda: self._chrome_copy_lbl.setText(""))
        if hasattr(self, "_firefox_copy_lbl"):
            self._firefox_copy_lbl.setText(feedback)
            QTimer.singleShot(3500, lambda: self._firefox_copy_lbl.setText(""))

    def _on_copy_extension_path(self):
        self._on_browser_url_clicked("extension_path")

    def _on_copy_chrome_url(self):
        self._on_browser_url_clicked("chrome://extensions/")

    def _on_copy_edge_url(self):
        self._on_browser_url_clicked("edge://extensions/")

    def _on_copy_firefox_url(self):
        self._on_browser_url_clicked("about:debugging#/runtime/this-firefox")

    def _on_copy_firefox_addons_url(self):
        self._on_browser_url_clicked("about:addons")

    def _on_open_chrome_extensions(self):
        self._on_browser_url_clicked("chrome://extensions/")


    def _on_package_firefox_extension(self):
        try:
            from my_idm.extension_packager import package_firefox_extension
            xpi_path = package_firefox_extension()
            res = QMessageBox.information(
                self,
                "Firefox Package Created",
                f"Firefox add-on packaged successfully:\n\n{xpi_path}\n\n"
                "To make this add-on permanent across browser restarts:\n"
                "• Firefox Developer / ESR / LibreWolf / Floorp: Set xpinstall.signatures.required=false in about:config, then install this .xpi in about:addons.\n"
                "• Standard Firefox Release: Upload this .xpi to addons.mozilla.org (AMO) Developer Hub for free automated unlisted signing.\n\n"
                "Would you like to open the folder containing this .xpi?",
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Ok,
            )
            if res == QMessageBox.StandardButton.Open:
                ok, msg = show_in_folder(xpi_path)
                if not ok:
                    QMessageBox.warning(self, "Folder Error", msg)
        except Exception as e:
            QMessageBox.critical(self, "Packaging Error", f"Failed to package Firefox extension:\n{e}")

    def _on_open_firefox_guide(self):
        guide = FirefoxInstallGuideDialog(self)
        guide.exec()

    # -----------------------------------------------------------------------
    # Population & Handlers
    # -----------------------------------------------------------------------

    def _populate_fields(self):
        # General tab
        self._save_path_edit.setText(self._general_cfg.default_save_path)
        self._remember_last_cb.setChecked(self._general_cfg.remember_last_save_path)
        self._segments_spin.setValue(self._general_cfg.default_segments)
        self._segment_stagger_spin.setValue(self._general_cfg.segment_start_delay_ms)
        self._concurrent_spin.setValue(self._general_cfg.max_concurrent_downloads)
        self._retries_spin.setValue(self._general_cfg.max_retries)
        self._retry_exp_cb.setChecked(self._general_cfg.retry_exponential_backoff)
        self._retry_delay_spin.setValue(self._general_cfg.retry_delay)
        self._retry_factor_spin.setValue(self._general_cfg.retry_backoff_factor)
        self._retry_max_delay_spin.setValue(int(self._general_cfg.retry_max_delay))
        self._on_retry_exp_toggled(self._general_cfg.retry_exponential_backoff)
        self._disk_space_check_cb.setChecked(self._general_cfg.disk_space_check)
        self._disk_space_headroom_spin.setValue(self._general_cfg.disk_space_headroom_mb)
        self._disk_space_headroom_spin.setEnabled(self._general_cfg.disk_space_check)


        # Backlog locations
        self._backlog_list.clear()
        for loc in self._general_cfg.get_effective_backlog_locations():
            self._backlog_list.addItem(loc)
        self._clear_backlog_cb.setChecked(self._general_cfg.clear_backlog_after_load)
        self._backlog_poll_cb.setChecked(self._general_cfg.backlog_poll_enabled)
        self._backlog_poll_spin.setValue(self._general_cfg.backlog_poll_interval)
        self._backlog_poll_spin.setEnabled(self._general_cfg.backlog_poll_enabled)

        # Application & Tray tab
        self._auto_resume_cb.setChecked(self._general_cfg.auto_resume_startup)
        self._notify_cb.setChecked(self._general_cfg.notify_on_completion)
        self._enable_system_tray_cb.setChecked(self._general_cfg.enable_system_tray)
        self._minimize_to_tray_cb.setChecked(self._general_cfg.minimize_to_tray)
        self._close_to_tray_cb.setChecked(self._general_cfg.close_to_tray)
        self._start_minimized_cb.setChecked(self._general_cfg.start_minimized)
        self._launch_at_login_cb.setChecked(self._general_cfg.launch_at_login)
        # Remembered so `_on_save` can tell "the user asked for this" from "the user left the box
        # alone and hit Save". Without it, every Save would re-register a login item the user had
        # deliberately removed through Task Manager or their desktop's startup panel.
        self._launch_at_login_as_loaded = self._general_cfg.launch_at_login
        self._minimize_to_tray_cb.setEnabled(self._general_cfg.enable_system_tray)
        self._close_to_tray_cb.setEnabled(self._general_cfg.enable_system_tray)
        self._start_minimized_cb.setEnabled(self._general_cfg.enable_system_tray)
        self._capture_hotkey_cb.setChecked(self._general_cfg.capture_hotkey_enabled)
        self._capture_hotkey_edit.setKeySequence(
            QKeySequence(self._general_cfg.capture_hotkey_sequence or "Ctrl+Alt+D")
        )
        self._on_capture_hotkey_toggled(self._general_cfg.capture_hotkey_enabled)
        self._refresh_autostart_status()

        # Clipboard tab
        self._clipboard_monitor_cb.setChecked(
            self._general_cfg.clipboard_monitor_enabled
        )
        self._clipboard_max_urls_spin.setValue(
            self._general_cfg.clipboard_monitor_max_urls
        )
        self._clipboard_min_size_spin.setValue(
            self._general_cfg.clipboard_min_file_size_kb
        )
        self._clipboard_ignored_exts_edit.setText(
            ", ".join(self._general_cfg.clipboard_ignored_extensions)
        )
        self._on_clipboard_monitor_toggled(
            self._general_cfg.clipboard_monitor_enabled
        )

        # BitTorrent tab
        self._seeding_after_complete_cb.setChecked(self._torrent_cfg.seeding_after_complete)
        self._resume_seeding_cb.setChecked(self._torrent_cfg.resume_seeding_on_startup)
        self._seeding_time_spin.setValue(self._torrent_cfg.seeding_time_limit_minutes)
        self._seeding_ratio_limit_spin.setValue(self._torrent_cfg.seeding_ratio_limit)
        self._max_seeding_speed_spin.setValue(self._torrent_cfg.max_seeding_speed)
        self._seeding_ratio_spin.setValue(self._torrent_cfg.download_to_seeding_ratio)
        self._metadata_timeout_spin.setValue(self._torrent_cfg.metadata_fetch_timeout_days)

        # .torrent file integration. The association checkbox records what was loaded so a Save
        # that did not touch it can be distinguished from one that did — see `_on_save`.
        self._torrent_assoc_cb.setChecked(self._torrent_cfg.associate_torrent_files)
        self._torrent_assoc_as_loaded = self._torrent_cfg.associate_torrent_files
        self._torrent_watch_cb.setChecked(self._torrent_cfg.watch_torrent_folder)
        self._torrent_watch_clean_cb.setChecked(self._torrent_cfg.clean_watched_torrent_files)
        self._torrent_watch_max_age_spin.setValue(self._torrent_cfg.torrent_watch_max_age_days)
        # An empty stored value means "the default download folder", which is exactly what the
        # effective resolver returns, so the field is seeded from it rather than left blank. A
        # blank field with a greyed Browse button reads as a setting that cannot be changed.
        self._torrent_watch_edit.setText(
            self._torrent_cfg.torrent_watch_folder
            or self._general_cfg.get_effective_save_path()
        )
        self._on_torrent_watch_toggled(self._torrent_cfg.watch_torrent_folder)
        self._refresh_torrent_assoc_status()

        # Network tab. Only the default entry is seeded here; enumerating adapters walks psutil
        # and is done by the probe thread (see `_start_probe`).
        self._reset_interface_combo()

        self._kill_switch_cb.setChecked(self._network_cfg.kill_switch)
        self._proxy_enable_cb.setChecked(self._network_cfg.proxy_enabled)
        idx = 1 if self._network_cfg.proxy_type == "socks5" else 0
        self._proxy_type_combo.setCurrentIndex(idx)
        self._proxy_port_spin.setValue(self._network_cfg.proxy_port or 8080)
        self._proxy_host_edit.setText(self._network_cfg.proxy_host)
        self._proxy_user_edit.setText(self._network_cfg.proxy_username)
        self._proxy_pass_edit.setText(self._network_cfg.proxy_password)
        self._on_proxy_toggled(self._network_cfg.proxy_enabled)

        # Security tab
        self._scan_before_cb.setChecked(self._security_cfg.scan_before_download)
        self._warn_ext_cb.setChecked(self._security_cfg.warn_high_risk_extensions)
        self._block_dangerous_cb.setChecked(self._security_cfg.block_dangerous_urls)
        self._vt_key_edit.setText(self._security_cfg.virustotal_api_key)

        self._scan_after_cb.setChecked(self._security_cfg.scan_after_download)
        if self._security_cfg.scanner_type == "custom":
            self._custom_rb.setChecked(True)
        else:
            self._defender_rb.setChecked(True)
        self._custom_scanner_edit.setText(self._security_cfg.custom_scanner_path)
        self._custom_args_edit.setText(self._security_cfg.custom_scanner_args or '"%file%"')

        if self._security_cfg.action_on_threat in ("quarantine", "delete"):
            self._action_quarantine_rb.setChecked(True)
        else:
            self._action_warn_rb.setChecked(True)

        # Scan timing
        if self._security_cfg.scan_timing == "manual_only":
            self._timing_manual_rb.setChecked(True)
        else:
            self._timing_auto_rb.setChecked(True)

        # Threat exclusions list
        self._threat_excl_list.clear()
        for cat in self._security_cfg.get_effective_threat_exclusions():
            if cat.strip():
                self._threat_excl_list.addItem(cat.strip())
        if self._security_cfg.ignored_threat_patterns:
            patterns = self._security_cfg.ignored_threat_patterns
            if isinstance(patterns, str):
                patterns = [p.strip() for p in patterns.split(",") if p.strip()]
            for pat in patterns:
                existing = [
                    self._threat_excl_list.item(i).text().strip().lower()
                    for i in range(self._threat_excl_list.count())
                ]
                if pat.lower() not in existing:
                    self._threat_excl_list.addItem(pat)

        # Tor tab
        self._tor_enable_cb.setChecked(self._tor_cfg.enabled)
        self._tor_autostart_cb.setChecked(self._tor_cfg.auto_start_at_startup)
        self._tor_route_http_cb.setChecked(self._tor_cfg.route_http)
        self._tor_route_torrent_cb.setChecked(self._tor_cfg.route_torrent)
        self._tor_host_edit.setText(self._tor_cfg.proxy_host)
        self._tor_port_spin.setValue(self._tor_cfg.proxy_port or 9050)
        # The detected executable is filled in by the probe thread; prefill only the saved value
        # so a user-typed path is never overwritten by a filesystem walk.
        detected_tor = self._tor_cfg.tor_executable_path or ""
        self._tor_path_edit.setText(detected_tor)

        # External Tools tab
        self._animepahe_repo_edit.setText(self._external_tools_cfg.animepahe_repo_path)
        self._animepahe_startup_cb.setChecked(self._external_tools_cfg.animepahe_launch_on_startup)
        self._animepahe_periodic_cb.setChecked(self._external_tools_cfg.animepahe_periodic_run)
        self._animepahe_interval_spin.setValue(self._external_tools_cfg.animepahe_interval_hours)
        self._animepahe_interval_spin.setEnabled(self._external_tools_cfg.animepahe_periodic_run)
        self._animepahe_interval_lbl.setEnabled(self._external_tools_cfg.animepahe_periodic_run)
        self._animepahe_url_edit.setText(self._external_tools_cfg.animepahe_last_url)
        self._animepahe_episodes_edit.setText(self._external_tools_cfg.animepahe_last_episodes)
        q_idx = self._animepahe_quality_combo.findText(self._external_tools_cfg.animepahe_last_quality)
        if q_idx >= 0:
            self._animepahe_quality_combo.setCurrentIndex(q_idx)
        l_idx = self._animepahe_lang_combo.findText(self._external_tools_cfg.animepahe_last_lang)
        if l_idx >= 0:
            self._animepahe_lang_combo.setCurrentIndex(l_idx)
        is_running = bool(self._manager and hasattr(self._manager, "is_animepahe_running") and self._manager.is_animepahe_running())
        q_raw = self._manager.get_animepahe_queue_length() if (self._manager and hasattr(self._manager, "get_animepahe_queue_length")) else 0
        try:
            q_len = int(q_raw)
        except (TypeError, ValueError):
            q_len = 0
        if is_running:
            self._btn_run_cli_now.setText("⏹️ Stop CLI Scraper")
            self._btn_run_cli_now.setToolTip("Stop running AnimePahe background scraper and clear queue")
            if hasattr(self, "_btn_download_animepahe_url"):
                self._btn_download_animepahe_url.setEnabled(True)
                self._btn_download_animepahe_url.setText("➕ Queue Anime Download")
        else:
            self._btn_run_cli_now.setText("▶️ Run CLI Now")
            self._btn_run_cli_now.setToolTip("Launch AnimePahe background scraper in CLI mode")
            if hasattr(self, "_btn_download_animepahe_url"):
                self._btn_download_animepahe_url.setEnabled(True)
                self._btn_download_animepahe_url.setText("⬇️ Download via AnimePahe")
        self._update_animepahe_queue_badge(q_len)

        # Browser Integration tab
        self._browser_enabled_cb.setChecked(self._browser_cfg.enabled)
        self._browser_port_spin.setValue(self._browser_cfg.port or 19582)
        self._browser_intercept_cb.setChecked(self._browser_cfg.intercept_all)
        self._browser_intercept_torrent_cb.setChecked(self._browser_cfg.intercept_torrent_files)
        self._browser_intercept_magnet_cb.setChecked(self._browser_cfg.intercept_magnet_links)
        self._browser_min_size_spin.setValue(self._browser_cfg.min_file_size_kb)
        self._browser_skip_unknown_size_cb.setChecked(
            self._browser_cfg.skip_unknown_size_downloads
        )
        self._browser_skip_unknown_size_cb.setEnabled(self._browser_cfg.min_file_size_kb > 0)
        self._browser_bypass_edit.setText(", ".join(self._browser_cfg.bypassed_extensions))
        if self._manager and getattr(self._manager, "browser_server", None) and self._manager.browser_server.is_running:
            self._browser_status_lbl.setText(f"🟢 Active (Listening on http://127.0.0.1:{self._browser_cfg.port})")
            self._browser_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold; margin-left: 12px;")
        elif self._browser_cfg.enabled:
            self._browser_status_lbl.setText("⚪ Server will start on apply")
            self._browser_status_lbl.setStyleSheet("color: #f1fa8c; margin-left: 12px;")
        else:
            self._browser_status_lbl.setText("⚪ Disabled")
            self._browser_status_lbl.setStyleSheet("color: #8fa0b5; margin-left: 12px;")

        # Scheduler tab
        self._scheduler_enable_cb.setChecked(self._scheduler_cfg.enabled)
        sh, sm = (int(x) for x in self._scheduler_cfg.start_time.split(":")[:2]) if ":" in self._scheduler_cfg.start_time else (2, 0)
        eh, em = (int(x) for x in self._scheduler_cfg.end_time.split(":")[:2]) if ":" in self._scheduler_cfg.end_time else (8, 0)
        self._scheduler_start_time.setTime(QTime(sh, sm))
        self._scheduler_end_time.setTime(QTime(eh, em))
        self._scheduler_pause_cb.setChecked(self._scheduler_cfg.pause_when_ended)
        active_days = set(self._scheduler_cfg.days_of_week)
        for day_idx, cb in enumerate(self._scheduler_day_cbs):
            cb.setChecked(day_idx in active_days)
        self._on_scheduler_enable_toggled(self._scheduler_cfg.enabled)

    def _on_test_tor(self):
        host = self._tor_host_edit.text().strip() or "127.0.0.1"
        port = self._tor_port_spin.value()
        self._tor_test_status_lbl.setText("Testing connection…")
        self._tor_test_status_lbl.setStyleSheet("color: #8be9fd;")
        if is_tor_reachable(host, port):
            self._tor_test_status_lbl.setText(f"✓ Connected to Tor proxy on {host}:{port}")
            self._tor_test_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold;")
        else:
            other_port = 9150 if port == 9050 else 9050
            other_desc = "Tor Browser" if other_port == 9150 else "Tor Service"
            if is_tor_reachable(host, other_port):
                self._tor_test_status_lbl.setText(
                    f"Port {port} not running, but {other_desc} is active on port {other_port}! Click '{other_desc} (Port {other_port})' above to use it."
                )
                self._tor_test_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold;")
            else:
                exe = find_tor_executable(self._tor_path_edit.text().strip())
                if exe:
                    self._tor_test_status_lbl.setText(f"Tor service not running. Executable found (will auto-start): {exe}")
                    self._tor_test_status_lbl.setStyleSheet("color: #f1fa8c;")
                else:
                    self._tor_test_status_lbl.setText(f"✗ Tor proxy not reachable and tor.exe not found")
                    self._tor_test_status_lbl.setStyleSheet("color: #ff5555; font-weight: bold;")

    def _on_browse_animepahe_repo(self):
        cur = self._animepahe_repo_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Select AnimePahe Repository Directory", cur)
        if path:
            self._animepahe_repo_edit.setText(normalize_path(path))

    def _on_open_animepahe_folder(self):
        target = self._animepahe_repo_edit.text().strip()
        if not target or not os.path.isdir(target):
            QMessageBox.warning(self, "Folder Not Found", f"The directory does not exist:\n{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _on_view_animepahe_console_log(self):
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        log_path = self._external_tools_cfg.get_console_log_path()
        ok, msg = open_file_in_default_app(log_path, create_if_missing=True)
        if not ok:
            QMessageBox.warning(self, "Cannot Open Console Log", msg)

    def _on_view_animepahe_debug_log(self):
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        log_path = self._external_tools_cfg.get_debug_log_path()
        ok, msg = open_file_in_default_app(log_path, create_if_missing=True)
        if not ok:
            QMessageBox.warning(self, "Cannot Open Debug Log", msg)

    def _on_launch_animepahe_gui_from_settings(self):
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        ok, msg = launch_animepahe_gui(self._external_tools_cfg)
        if not ok:
            QMessageBox.warning(self, "Launch Failed", msg)

    def _on_run_animepahe_cli_from_settings(self):
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        repo = self._external_tools_cfg.get_effective_repo_path()
        if not repo or not os.path.isdir(repo):
            QMessageBox.warning(
                self,
                "Repository Not Found",
                f"AnimePahe repository directory does not exist:\n{self._external_tools_cfg.animepahe_repo_path}",
            )
            return

        if self._manager is not None:
            self._manager.set_external_tools_config(self._external_tools_cfg)
            if self._manager.is_animepahe_running():
                ok, msg = self._manager.stop_animepahe_scraper()
            else:
                ok, msg = self._manager.start_animepahe_scraper()
        else:
            ok, msg, proc = launch_animepahe_cli(self._external_tools_cfg)

        if not ok:
            QMessageBox.warning(self, "CLI Scraper", msg)

    def _on_download_animepahe_url(self):
        url = self._animepahe_url_edit.text().strip()
        episodes = self._animepahe_episodes_edit.text().strip()

        if not url:
            QMessageBox.warning(self, "Missing URL", "Please enter an AnimePahe anime or episode URL.")
            self._animepahe_url_edit.setFocus()
            return

        # Direct UUID or numeric ID support (e.g. 4380 or ef667bb4-3a9b-449e-1a22-26156a642e47)
        if re.match(r'^[a-f0-9-]+$', url, re.IGNORECASE):
            url = f"https://animepahe.ru/anime/{url}"
        elif not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Normalize /play/ or /a/ URLs to /anime/<id>
        play_or_a_match = re.search(r'/(?:play|a)/([a-f0-9-]+)', url, re.IGNORECASE)
        if play_or_a_match and '/anime/' not in url:
            domain_match = re.search(r'https?://([^/]+)', url)
            domain = domain_match.group(1) if domain_match else "animepahe.ru"
            aid = play_or_a_match.group(1)
            url = f"https://{domain}/anime/{aid}"

        # Validate episodes format if provided
        if episodes:
            if not re.match(r'^[\d\s,-]+$', episodes):
                QMessageBox.warning(
                    self,
                    "Invalid Episode Range",
                    "Please enter a valid episode range, e.g.:\n• 1-12\n• 1, 3, 5-10\n• 25\n\nOr leave blank to download all episodes.",
                )
                self._animepahe_episodes_edit.setFocus()
                return
            cleaned_parts = []
            for part in episodes.split(','):
                part = part.strip()
                if '-' in part:
                    sub = [s.strip() for s in part.split('-') if s.strip()]
                    cleaned_parts.append('-'.join(sub))
                elif part:
                    cleaned_parts.append(part)
            episodes = ', '.join(cleaned_parts)

        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        self._external_tools_cfg.animepahe_last_url = url
        self._external_tools_cfg.animepahe_last_episodes = episodes
        self._external_tools_cfg.animepahe_last_quality = self._animepahe_quality_combo.currentText()
        self._external_tools_cfg.animepahe_last_lang = self._animepahe_lang_combo.currentText()
        self._external_tools_cfg.save()

        repo = self._external_tools_cfg.get_effective_repo_path()
        if not repo or not os.path.isdir(repo):
            QMessageBox.warning(
                self,
                "Repository Not Found",
                f"AnimePahe repository directory does not exist:\n{self._external_tools_cfg.animepahe_repo_path}",
            )
            return

        q_val = self._animepahe_quality_combo.currentText()
        quality = q_val if q_val != "Auto" else None

        l_val = self._animepahe_lang_combo.currentText()
        lang = "en" if "Dub" in l_val else ("jap" if "Sub" in l_val else None)

        if self._manager is not None:
            self._manager.set_external_tools_config(self._external_tools_cfg)
            ok, msg = self._manager.start_animepahe_scraper(
                url=url,
                episodes=episodes or None,
                quality=quality,
                lang=lang,
            )
        else:
            ok, msg, proc = launch_animepahe_cli(
                self._external_tools_cfg,
                url=url,
                episodes=episodes or None,
                quality=quality,
                lang=lang,
            )

        if ok:
            # No confirm dialog: closing Preferences here lets the caller (the main
            # window) react to the accepted result and switch the bottom panel to the
            # live AnimePahe console, which is where the scraper's progress is visible.
            self.animepahe_download_started = True
            self.accept()
        else:
            QMessageBox.warning(self, "Download Failed to Start", msg)

    def _on_animepahe_status_changed(self, is_running: bool):
        if hasattr(self, "_btn_run_cli_now"):
            if is_running:
                self._btn_run_cli_now.setText("⏹️ Stop CLI Scraper")
                self._btn_run_cli_now.setToolTip("Stop running AnimePahe background scraper and clear queue")
            else:
                self._btn_run_cli_now.setText("▶️ Run CLI Now")
                self._btn_run_cli_now.setToolTip("Launch AnimePahe background scraper in CLI mode")
        if hasattr(self, "_btn_download_animepahe_url"):
            self._btn_download_animepahe_url.setEnabled(True)
            self._btn_download_animepahe_url.setText("➕ Queue Anime Download" if is_running else "⬇️ Download via AnimePahe")
        q_raw = self._manager.get_animepahe_queue_length() if (self._manager and hasattr(self._manager, "get_animepahe_queue_length")) else 0
        try:
            q_len = int(q_raw)
        except (TypeError, ValueError):
            q_len = 0
        self._update_animepahe_queue_badge(q_len)

    def _on_animepahe_queue_changed(self, count: Any):
        self._update_animepahe_queue_badge(count)

    def _update_animepahe_queue_badge(self, count: Any):
        if hasattr(self, "_animepahe_queue_lbl"):
            try:
                cnt = int(count)
            except (TypeError, ValueError):
                cnt = 0
            if cnt > 0:
                self._animepahe_queue_lbl.setText(f"📋 Queued: {cnt}")
                self._animepahe_queue_lbl.setVisible(True)
            else:
                self._animepahe_queue_lbl.setText("")
                self._animepahe_queue_lbl.setVisible(False)

    def _on_browse_tor_path(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Tor Executable", "",
            "Executable Files (*.exe);;All Files (*)",
        )
        if path:
            self._tor_path_edit.setText(path)

    def _on_browse_default_path(self):
        cur = self._save_path_edit.text().strip() or DEFAULT_DOWNLOADS_DIR
        path = QFileDialog.getExistingDirectory(self, "Select Default Download Folder", cur)
        if path:
            self._save_path_edit.setText(path)

    def _on_open_default_path(self):
        target = self._save_path_edit.text().strip() or DEFAULT_DOWNLOADS_DIR
        if not os.path.exists(target):
            try:
                os.makedirs(target, exist_ok=True)
            except Exception as ex:
                QMessageBox.warning(self, "Cannot Open Folder", f"Failed to create directory:\n{ex}")
                return
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _on_add_backlog_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Backlog Folder")
        if folder:
            norm = normalize_path(folder)
            existing = [self._backlog_list.item(i).text() for i in range(self._backlog_list.count())]
            if norm not in existing:
                self._backlog_list.addItem(norm)

    def _on_add_backlog_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Backlog File", "", "Text Files (*.txt);;All Files (*)"
        )
        if file_path:
            norm = normalize_path(file_path)
            existing = [self._backlog_list.item(i).text() for i in range(self._backlog_list.count())]
            if norm not in existing:
                self._backlog_list.addItem(norm)

    def _on_remove_backlog_loc(self):
        for item in self._backlog_list.selectedItems():
            self._backlog_list.takeItem(self._backlog_list.row(item))

    def _on_reset_backlog_defaults(self):
        self._backlog_list.clear()
        defaults = [
            normalize_path(Path.cwd()),
            normalize_path(APP_DIR),
            normalize_path(Path.home()),
        ]
        for d in defaults:
            self._backlog_list.addItem(d)

    def _reset_interface_combo(self):
        """Seed the combo with just the default entry. Cheap - no system calls.

        Deliberately does *not* enumerate interfaces: that is a synchronous psutil walk costing
        ~25 ms, and it belongs on the probe thread (see `_PrefsProbeWorker`).

        Keeping this as the only synchronous content is what makes the deferred populate safe. Every
        consumer of `self._interfaces` (`_on_iface_changed`, the bind-IP lookup, the save path)
        treats index 0 as "all interfaces", so a combo holding only index 0 can never index an empty
        list.
        """
        self._interfaces = []
        self._iface_combo.blockSignals(True)
        self._iface_combo.clear()
        self._iface_combo.addItem("🌐 All Interfaces (Default / Automatic)", "")
        self._iface_combo.setCurrentIndex(0)
        self._iface_combo.blockSignals(False)
        self._iface_details_label.setText("Checking available interfaces…")

    def _apply_interfaces(self, interfaces):
        """Fill the interface combo from probe results.

        Enumerating adapters is not free and touches the network stack, so it runs off the GUI
        thread. Only widgets are touched here, and only on the GUI thread, because Qt requires it.
        """
        self._interfaces = list(interfaces)
        self._iface_combo.blockSignals(True)
        self._iface_combo.clear()
        self._iface_combo.addItem("🌐 All Interfaces (Default / Automatic)", "")

        selected_idx = 0
        for i, iface in enumerate(self._interfaces, start=1):
            flag = " [VPN]" if iface.is_vpn else ""
            status = "🟢" if iface.is_up else "⚪"
            label = f"{status} {iface.name}{flag} ({iface.ip})"
            self._iface_combo.addItem(label, iface.name)
            if iface.name == self._network_cfg.interface_name:
                selected_idx = i

        self._iface_combo.setCurrentIndex(selected_idx)
        self._iface_combo.blockSignals(False)
        self._on_iface_changed(selected_idx)

    def _load_interfaces(self):
        """Enumerate adapters on the calling thread and apply the result.

        Kept synchronous for the explicit Refresh button, where the user has asked for it and a
        brief freeze is the expected cost.
        """
        from my_idm.network import get_available_interfaces

        self._apply_interfaces(get_available_interfaces())

    def _on_system_tray_toggled(self, checked: bool):
        self._minimize_to_tray_cb.setEnabled(checked)
        self._close_to_tray_cb.setEnabled(checked)
        self._start_minimized_cb.setEnabled(checked)

    def _refresh_autostart_status(self):
        """Report the real registration state under the launch-at-login checkbox.

        The checkbox shows stored intent; this label shows what the OS will actually do. They can
        legitimately differ - the login item can be removed behind the app's back, or left pointing
        at a checkout that has moved - and a checkbox that merely echoes itself back would hide
        both.
        """
        from my_idm import autostart

        try:
            state = autostart.status()
        except Exception as exc:  # a probe failure must not break the preferences dialog
            self._autostart_status_lbl.setText(f"⚠️ Could not read the startup entry: {exc}")
            self._autostart_status_lbl.setStyleSheet("color: #e06c75; font-size: 11px;")
            self._autostart_repair_btn.setVisible(False)
            return

        where = autostart.location()

        if state is autostart.AutostartState.UNSUPPORTED:
            self._launch_at_login_cb.setEnabled(False)
            self._launch_at_login_cb.setChecked(False)
            self._autostart_status_lbl.setText(
                "⚠️ Launch at login is not supported on this platform. Add My-IDM to your "
                "session's startup applications by hand."
            )
            self._autostart_status_lbl.setStyleSheet("color: #e0af68; font-size: 11px;")
            self._autostart_repair_btn.setVisible(False)
        elif state is autostart.AutostartState.STALE:
            self._autostart_status_lbl.setText(
                f"⚠️ The saved login item points somewhere else and will not start My-IDM.\n{where}"
            )
            self._autostart_status_lbl.setStyleSheet("color: #e0af68; font-size: 11px;")
            self._autostart_repair_btn.setVisible(True)
        elif state is autostart.AutostartState.ENABLED:
            self._autostart_status_lbl.setText(f"✅ Will start when you log in.\n{where}")
            self._autostart_status_lbl.setStyleSheet("color: #7ee787; font-size: 11px;")
            self._autostart_repair_btn.setVisible(False)
        else:
            self._autostart_status_lbl.setText(
                f"Not registered. When enabled, the login item is written to:\n{where}"
            )
            self._autostart_status_lbl.setStyleSheet("color: #8fa0b5; font-size: 11px;")
            self._autostart_repair_btn.setVisible(False)

    def _on_repair_autostart(self):
        from my_idm import autostart

        ok, message = autostart.repair()
        self._refresh_autostart_status()
        if ok:
            if message:
                QMessageBox.information(self, "Startup Entry Repaired", message)
            else:
                QMessageBox.information(
                    self,
                    "Startup Entry Repaired",
                    "The login item now points at this copy of My-IDM.",
                )
        else:
            QMessageBox.warning(self, "Repair Failed", message)

    def _on_clipboard_monitor_toggled(self, checked: bool):
        self._clipboard_max_urls_spin.setEnabled(checked)
        self._clipboard_min_size_spin.setEnabled(checked)
        self._clipboard_ignored_exts_edit.setEnabled(checked)

    def _on_torrent_watch_toggled(self, checked: bool):
        self._torrent_watch_edit.setEnabled(checked)
        self._torrent_watch_browse_btn.setEnabled(checked)
        self._torrent_watch_clean_cb.setEnabled(checked)
        self._torrent_watch_max_age_lbl.setEnabled(checked)
        self._torrent_watch_max_age_spin.setEnabled(checked)

    def _on_browse_torrent_watch_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Folder to Watch for .torrent Files",
            self._torrent_watch_edit.text().strip() or DEFAULT_DOWNLOADS_DIR,
        )
        if folder:
            self._torrent_watch_edit.setText(folder)

    def _refresh_torrent_assoc_status(self):
        """Report the real .torrent association state under its checkbox.

        Mirrors :meth:`_refresh_autostart_status`, with one extra state that the launch-at-login
        control does not need: ``REGISTERED`` without ``DEFAULT``. On Windows that is the normal
        permanent condition — the ProgID is ours and correct, and Windows still hands the file to
        another program — and collapsing it into "on" would tell the user double-clicking works
        when it does not.
        """
        from my_idm import file_assoc

        try:
            state = file_assoc.status()
            where = file_assoc.location()
        except Exception as exc:  # a probe failure must not break the preferences dialog
            self._torrent_assoc_status_lbl.setText(f"⚠️ Could not read the association: {exc}")
            self._torrent_assoc_status_lbl.setStyleSheet("color: #e06c75; font-size: 11px;")
            self._torrent_assoc_repair_btn.setVisible(False)
            self._torrent_assoc_default_btn.setVisible(False)
            return

        label = self._torrent_assoc_status_lbl
        # The "make it the default" button is only meaningful where the OS lets a person do that
        # and the app cannot. Elsewhere it would open a page with nothing to click.
        can_choose = file_assoc.backend_name() == "windows"
        self._torrent_assoc_default_btn.setVisible(can_choose)

        if state is file_assoc.FileAssocState.UNSUPPORTED:
            self._torrent_assoc_cb.setEnabled(False)
            self._torrent_assoc_cb.setChecked(False)
            label.setText(
                "⚠️ My-IDM cannot register itself for .torrent files on this platform. "
                "Choose My-IDM from your file manager's 'Open with' menu."
            )
            label.setStyleSheet("color: #e0af68; font-size: 11px;")
            self._torrent_assoc_repair_btn.setVisible(False)
        elif state is file_assoc.FileAssocState.STALE:
            label.setText(
                f"⚠️ The saved .torrent entry points somewhere else and will not open "
                f"My-IDM.\n{where}"
            )
            label.setStyleSheet("color: #e0af68; font-size: 11px;")
            self._torrent_assoc_repair_btn.setVisible(True)
        elif state is file_assoc.FileAssocState.DEFAULT:
            label.setText(f"✅ .torrent files open in My-IDM.\n{where}")
            label.setStyleSheet("color: #7ee787; font-size: 11px;")
            self._torrent_assoc_repair_btn.setVisible(False)
        elif state is file_assoc.FileAssocState.REGISTERED:
            label.setText(
                "⚠️ My-IDM is registered for .torrent files but is not the default, so "
                "double-clicking still opens another program. Choose My-IDM once in the "
                "system's Default Apps settings.\n" + where
            )
            label.setStyleSheet("color: #e0af68; font-size: 11px;")
            self._torrent_assoc_repair_btn.setVisible(False)
        else:
            label.setText(
                f"Not registered. When enabled, the handler is written to:\n{where}"
            )
            label.setStyleSheet("color: #8fa0b5; font-size: 11px;")
            self._torrent_assoc_repair_btn.setVisible(False)

    def _on_repair_torrent_assoc(self):
        from my_idm import file_assoc

        ok, message = file_assoc.repair()
        self._refresh_torrent_assoc_status()
        if ok:
            QMessageBox.information(
                self,
                "Registration Repaired",
                message
                or "The .torrent registration now points at this copy of My-IDM.",
            )
        else:
            QMessageBox.warning(self, "Repair Failed", message)

    def _on_open_default_apps(self):
        """Send the user to the one place the .torrent default can actually be changed.

        `ms-settings:defaultapps?registeredAppUser=` deep-links to this application's entry on
        Windows 10 1809+; older builds ignore the query and show the full list, which is still a
        usable answer. The plain scheme is the fallback so a host that rejects the parameter does
        not produce a dead button.
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        for target in (
            "ms-settings:defaultapps?registeredAppUser=My-IDM",
            "ms-settings:defaultapps",
        ):
            if QDesktopServices.openUrl(QUrl(target)):
                return
        QMessageBox.information(
            self,
            "Default Apps",
            "Open Settings > Apps > Default apps, choose My-IDM, and set it for .torrent files.",
        )

    def _on_browser_min_size_changed(self, value: int):
        """The unknown-size choice only means anything while a minimum is set.

        With no minimum every size qualifies, so the checkbox would be a control with no
        effect - and a user could turn it off there, believe they had relaxed a limit, and
        change nothing.
        """
        self._browser_skip_unknown_size_cb.setEnabled(value > 0)

    def _on_capture_hotkey_toggled(self, checked: bool):
        self._capture_hotkey_edit.setEnabled(checked)
        if checked:
            self._validate_capture_hotkey()

    def _on_capture_hotkey_edited(self):
        self._validate_capture_hotkey()

    def _validate_capture_hotkey(self) -> bool:
        """Report whether the chosen chord is usable, inline and without a modal dialog.

        Inline because this is a live-editable field, not a submit-and-wait one: a ``QMessageBox``
        per keystroke would be hostile, and ``QKeySequenceEdit`` commits on every ``editingFinished``.
        The validation itself is shared with the runtime registration path so the dialog cannot
        accept a chord the hotkey layer will later refuse.
        """
        from my_idm.hotkey import parse_hotkey

        text = self._capture_hotkey_edit.keySequence().toString()
        if parse_hotkey(text) is None:
            self._capture_hotkey_status_lbl.setStyleSheet("color: #e06c75; font-size: 11px;")
            self._capture_hotkey_status_lbl.setText(
                f"'{text}' is not a usable global hotkey — it needs Ctrl, Alt or Win."
                if text else "Pick a key combination."
            )
            return False
        self._capture_hotkey_status_lbl.setStyleSheet("color: #a0a0a0; font-size: 11px;")
        self._capture_hotkey_status_lbl.setText(
            f"'{text}' will toggle download capture from any application."
        )
        return True

    def _on_retry_exp_toggled(self, checked: bool):
        self._retry_factor_lbl.setEnabled(checked)
        self._retry_factor_spin.setEnabled(checked)
        self._retry_max_delay_lbl.setEnabled(checked)
        self._retry_max_delay_spin.setEnabled(checked)

    def _on_iface_changed(self, index: int):
        if index <= 0:
            self._iface_details_label.setText("Traffic will use default system routing.")
            return
        iface = self._interfaces[index - 1]
        vpn_txt = "Yes (Virtual/Tunnel Adapter)" if iface.is_vpn else "No (Standard Interface)"
        status_txt = "UP / Active" if iface.is_up else "DOWN / Inactive"
        self._iface_details_label.setText(
            f"IP: {iface.ip} | Status: {status_txt} | VPN: {vpn_txt}"
        )

    def _on_proxy_toggled(self, enabled: bool):
        self._proxy_type_combo.setEnabled(enabled)
        self._proxy_port_spin.setEnabled(enabled)
        self._proxy_host_edit.setEnabled(enabled)
        self._proxy_user_edit.setEnabled(enabled)
        self._proxy_pass_edit.setEnabled(enabled)

    def _on_browse_scanner(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Antivirus Scanner Executable", "",
            "Executables (*.exe);;All Files (*)",
        )
        if path:
            self._custom_scanner_edit.setText(path)
            self._custom_rb.setChecked(True)

    def _on_test_network(self):
        proxy_en = self._proxy_enable_cb.isChecked()
        p_type = self._proxy_type_combo.currentText().lower()
        p_host = self._proxy_host_edit.text().strip()
        p_port = self._proxy_port_spin.value()
        p_user = self._proxy_user_edit.text().strip()
        p_pass = self._proxy_pass_edit.text()

        idx = self._iface_combo.currentIndex()
        bind_ip = self._interfaces[idx - 1].ip if idx > 0 else ""

        proxy_url = ""
        if proxy_en and p_host:
            auth = f"{p_user}:{p_pass}@" if p_user and p_pass else (f"{p_user}@" if p_user else "")
            proxy_url = f"{p_type}://{auth}{p_host}:{p_port}"

        async def _probe():
            connector = None
            if bind_ip:
                connector = aiohttp.TCPConnector(local_addr=(bind_ip, 0))
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                kwargs = {}
                if proxy_url:
                    kwargs["proxy"] = proxy_url
                async with session.get("https://httpbin.org/ip", **kwargs) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        return True, text
                    return False, f"HTTP Status {resp.status}"

        try:
            loop = asyncio.new_event_loop()
            success, message = loop.run_until_complete(_probe())
            loop.close()
            if success:
                QMessageBox.information(
                    self, "Connection Successful",
                    f"✅ Successfully connected to the internet!\n\nResponse:\n{message}"
                )
            else:
                QMessageBox.warning(
                    self, "Connection Failed",
                    f"❌ Test request returned an error:\n{message}"
                )
        except Exception as exc:
            QMessageBox.critical(
                self, "Connection Failed",
                f"❌ Could not connect via the selected configuration:\n{exc}"
            )

    def _on_test_scanner(self):
        scanner_type = "custom" if self._custom_rb.isChecked() else "defender"
        custom_path = self._custom_scanner_edit.text().strip()
        custom_args = self._custom_args_edit.text().strip()

        cfg = SecurityConfig(
            scan_after_download=True,
            scanner_type=scanner_type,
            custom_scanner_path=custom_path,
            custom_scanner_args=custom_args,
        )

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            tf.write(b"Safe test file for antivirus scanning verification.\n")
            temp_path = tf.name

        try:
            verdict, report = scan_file(temp_path, cfg)
            scanner_display = f"Custom ({Path(custom_path).name})" if scanner_type == "custom" and custom_path else "Windows Defender"
            if verdict is None:
                # Distinct from a threat: the scanner never ran, so the test verified nothing.
                # Reporting this as "scanner executed but detected a threat" would be a lie, and
                # reporting it as success would be worse.
                QMessageBox.critical(
                    self, "Antivirus Scanner Test",
                    f"⚠ The scanner could not be run, so the result is unknown.\n\n"
                    f"Scanner: {scanner_display}\n"
                    f"Details: {report}",
                )
            elif verdict:
                QMessageBox.information(
                    self, "Antivirus Scanner Test",
                    f"✅ Scanner verified successfully!\n\n"
                    f"Scanner: {scanner_display}\n"
                    f"Result: Clean (Safe)\n"
                    f"Details: {report}",
                )
            else:
                QMessageBox.warning(
                    self, "Antivirus Scanner Test",
                    f"⚠️ Scanner executed but detected a threat or returned non-zero code:\n\n"
                    f"Scanner: {scanner_display}\n"
                    f"Result: {report}",
                )
        except Exception as ex:
            QMessageBox.critical(
                self, "Antivirus Test Error",
                f"❌ Failed to run scanner:\n\n{ex}",
            )
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def _on_add_threat_exclusion(self):
        text = self._new_threat_excl_edit.text().strip()
        if not text:
            return
        items = [p.strip() for p in text.split(",") if p.strip()]
        existing = [
            self._threat_excl_list.item(i).text().strip().lower()
            for i in range(self._threat_excl_list.count())
        ]
        for item in items:
            if item.lower() not in existing:
                self._threat_excl_list.addItem(item)
                existing.append(item.lower())
        self._new_threat_excl_edit.clear()

    def _on_remove_threat_exclusion(self):
        row = self._threat_excl_list.currentRow()
        if row >= 0:
            self._threat_excl_list.takeItem(row)

    def _on_reset_threat_exclusions_defaults(self):
        self._threat_excl_list.clear()
        for cat in KNOWN_THREAT_CATEGORIES:
            self._threat_excl_list.addItem(cat)

    # -----------------------------------------------------------------------
    # Save & Results
    # -----------------------------------------------------------------------

    def _on_save(self):
        # 1. Validate & collect General settings
        save_path = self._save_path_edit.text().strip() or DEFAULT_DOWNLOADS_DIR
        if not os.path.exists(save_path):
            try:
                os.makedirs(save_path, exist_ok=True)
            except Exception as ex:
                QMessageBox.warning(
                    self, "Invalid Folder",
                    f"Could not create download directory:\n{save_path}\n\nError: {ex}",
                )
                return

        self._general_cfg.default_save_path = save_path
        self._general_cfg.last_save_path = save_path
        self._general_cfg.remember_last_save_path = self._remember_last_cb.isChecked()
        self._general_cfg.default_segments = self._segments_spin.value()
        self._general_cfg.segment_start_delay_ms = self._segment_stagger_spin.value()
        self._general_cfg.max_concurrent_downloads = self._concurrent_spin.value()
        self._general_cfg.max_retries = self._retries_spin.value()
        self._general_cfg.retry_exponential_backoff = self._retry_exp_cb.isChecked()
        self._general_cfg.retry_delay = self._retry_delay_spin.value()
        self._general_cfg.retry_backoff_factor = self._retry_factor_spin.value()
        self._general_cfg.retry_max_delay = float(self._retry_max_delay_spin.value())
        self._general_cfg.auto_resume_startup = self._auto_resume_cb.isChecked()
        self._general_cfg.notify_on_completion = self._notify_cb.isChecked()
        self._general_cfg.enable_system_tray = self._enable_system_tray_cb.isChecked()
        self._general_cfg.minimize_to_tray = self._minimize_to_tray_cb.isChecked()
        self._general_cfg.close_to_tray = self._close_to_tray_cb.isChecked()
        self._general_cfg.start_minimized = self._start_minimized_cb.isChecked()
        self._general_cfg.launch_at_login = self._launch_at_login_cb.isChecked()
        self._general_cfg.clipboard_monitor_enabled = self._clipboard_monitor_cb.isChecked()
        self._general_cfg.clipboard_monitor_max_urls = self._clipboard_max_urls_spin.value()
        self._general_cfg.clipboard_min_file_size_kb = self._clipboard_min_size_spin.value()
        self._general_cfg.clipboard_ignored_extensions = normalize_extension_list(
            self._clipboard_ignored_exts_edit.text()
        )
        self._general_cfg.capture_hotkey_enabled = self._capture_hotkey_cb.isChecked()
        self._general_cfg.capture_hotkey_sequence = (
            self._capture_hotkey_edit.keySequence().toString() or "Ctrl+Alt+D"
        )
        self._general_cfg.metadata_fetch_timeout_days = self._metadata_timeout_spin.value()
        self._general_cfg.disk_space_check = self._disk_space_check_cb.isChecked()
        self._general_cfg.disk_space_headroom_mb = self._disk_space_headroom_spin.value()
        locs = [self._backlog_list.item(i).text().strip() for i in range(self._backlog_list.count())]
        self._general_cfg.backlog_locations = [l for l in locs if l]
        self._general_cfg.clear_backlog_after_load = self._clear_backlog_cb.isChecked()
        self._general_cfg.backlog_poll_enabled = self._backlog_poll_cb.isChecked()
        self._general_cfg.backlog_poll_interval = self._backlog_poll_spin.value()
        self._general_cfg.save()

        # 1b. Apply the launch-at-login change to the operating system.
        #
        # Only when the checkbox actually moved. An unconditional sync here would re-create a login
        # item the user removed on purpose through Task Manager or their desktop's startup panel,
        # the next time they so much as opened Preferences and pressed Save. `reconcile` then does
        # the remaining repair work - notably rewriting a *stale* entry, which is broken rather than
        # disabled and so cannot have been an intentional removal.
        if self._launch_at_login_cb.isChecked() != self._launch_at_login_as_loaded:
            from my_idm import autostart

            autostart_ok, autostart_message = autostart.reconcile(self._launch_at_login_cb.isChecked())
            if not autostart_ok:
                # The preference is still recorded, so the user's intent survives a transient
                # failure (a locked registry hive, a read-only home). Losing the toggle would be a
                # worse lie than a failed registration the user is now told about.
                QMessageBox.warning(
                    self,
                    "Launch at Login Not Applied",
                    f"{autostart_message}\n\n"
                    "The preference has been saved and will be retried next time you change it.",
                )
            elif autostart_message:
                QMessageBox.information(
                    self, "Launch at Login", autostart_message
                )

        # 2. Collect BitTorrent settings
        self._torrent_cfg.seeding_after_complete = self._seeding_after_complete_cb.isChecked()
        self._torrent_cfg.resume_seeding_on_startup = self._resume_seeding_cb.isChecked()
        self._torrent_cfg.seeding_time_limit_minutes = self._seeding_time_spin.value()
        self._torrent_cfg.seeding_ratio_limit = self._seeding_ratio_limit_spin.value()
        self._torrent_cfg.max_seeding_speed = self._max_seeding_speed_spin.value()
        self._torrent_cfg.download_to_seeding_ratio = self._seeding_ratio_spin.value()
        self._torrent_cfg.metadata_fetch_timeout_days = self._metadata_timeout_spin.value()

        # 2b. .torrent file integration. The association is an OS-level change, so it is
        # reconciled only when the checkbox actually moved — the same rule the launch-at-login
        # control above follows. Reconciling unconditionally would resurrect an association the
        # user had removed in the system settings, merely because they opened Preferences and
        # pressed Save.
        self._torrent_cfg.associate_torrent_files = self._torrent_assoc_cb.isChecked()
        if self._torrent_cfg.associate_torrent_files != self._torrent_assoc_as_loaded:
            from my_idm import file_assoc

            assoc_ok, assoc_message = file_assoc.reconcile(
                self._torrent_cfg.associate_torrent_files
            )
            if not assoc_ok:
                # As above: the preference is recorded regardless, so the user's intent survives
                # a transient failure. Losing the toggle would be a worse lie than a failed
                # registration the user is now told about.
                QMessageBox.warning(
                    self,
                    "File Association Not Applied",
                    f"{assoc_message}\n\n"
                    "The preference has been saved and will be retried next time you change it.",
                )
            else:
                self._torrent_assoc_as_loaded = self._torrent_cfg.associate_torrent_files
                if assoc_message:
                    QMessageBox.information(self, "File Association", assoc_message)
                # Either way the label is now out of date, including after a *disable*.
                self._refresh_torrent_assoc_status()

        # The watched folder is resolved against the effective default download folder when the
        # field is left blank, so saving an untouched field records "the default" and follows a
        # later change to the download folder rather than pinning the path that was current when
        # the preference was first saved.
        self._torrent_cfg.watch_torrent_folder = self._torrent_watch_cb.isChecked()
        self._torrent_cfg.clean_watched_torrent_files = self._torrent_watch_clean_cb.isChecked()
        self._torrent_cfg.torrent_watch_max_age_days = self._torrent_watch_max_age_spin.value()
        watch_folder = self._torrent_watch_edit.text().strip()
        default_folder = self._general_cfg.get_effective_save_path()
        self._torrent_cfg.torrent_watch_folder = (
            "" if watch_folder == default_folder else watch_folder
        )
        self._torrent_cfg.save()

        # 3. Collect Network settings
        idx = self._iface_combo.currentIndex()
        if idx > 0 and idx - 1 < len(self._interfaces):
            self._network_cfg.interface_name = self._interfaces[idx - 1].name
            self._network_cfg.interface_ip = self._interfaces[idx - 1].ip
        else:
            self._network_cfg.interface_name = ""
            self._network_cfg.interface_ip = ""

        self._network_cfg.kill_switch = self._kill_switch_cb.isChecked()
        self._network_cfg.proxy_enabled = self._proxy_enable_cb.isChecked()
        self._network_cfg.proxy_type = self._proxy_type_combo.currentText().lower()
        self._network_cfg.proxy_host = self._proxy_host_edit.text().strip()
        self._network_cfg.proxy_port = self._proxy_port_spin.value()
        self._network_cfg.proxy_username = self._proxy_user_edit.text().strip()
        self._network_cfg.proxy_password = self._proxy_pass_edit.text()
        self._network_cfg.save()

        # 4. Collect Security settings
        self._security_cfg.scan_before_download = self._scan_before_cb.isChecked()
        self._security_cfg.warn_high_risk_extensions = self._warn_ext_cb.isChecked()
        self._security_cfg.block_dangerous_urls = self._block_dangerous_cb.isChecked()
        self._security_cfg.virustotal_api_key = self._vt_key_edit.text().strip()
        self._security_cfg.scan_after_download = self._scan_after_cb.isChecked()
        self._security_cfg.scanner_type = "custom" if self._custom_rb.isChecked() else "defender"
        self._security_cfg.custom_scanner_path = self._custom_scanner_edit.text().strip()
        self._security_cfg.custom_scanner_args = self._custom_args_edit.text().strip()
        self._security_cfg.action_on_threat = (
            "quarantine" if self._action_quarantine_rb.isChecked() else "warn"
        )
        self._security_cfg.scan_timing = (
            "manual_only" if self._timing_manual_rb.isChecked() else "after_complete"
        )
        excl_items = [
            self._threat_excl_list.item(i).text().strip()
            for i in range(self._threat_excl_list.count())
        ]
        self._security_cfg.ignored_threat_categories = [x for x in excl_items if x]
        self._security_cfg.ignored_threat_patterns = ""
        self._security_cfg.save()

        # 5. Collect Tor settings
        self._tor_cfg.enabled = self._tor_enable_cb.isChecked()
        self._tor_cfg.auto_start_at_startup = self._tor_autostart_cb.isChecked()
        self._tor_cfg.route_http = self._tor_route_http_cb.isChecked()
        self._tor_cfg.route_torrent = self._tor_route_torrent_cb.isChecked()
        self._tor_cfg.proxy_host = self._tor_host_edit.text().strip() or "127.0.0.1"
        self._tor_cfg.proxy_port = self._tor_port_spin.value()
        self._tor_cfg.tor_executable_path = self._tor_path_edit.text().strip()
        self._tor_cfg.save()

        # 6. Collect External Tools settings
        self._external_tools_cfg.animepahe_repo_path = self._animepahe_repo_edit.text().strip()
        self._external_tools_cfg.animepahe_launch_on_startup = self._animepahe_startup_cb.isChecked()
        self._external_tools_cfg.animepahe_periodic_run = self._animepahe_periodic_cb.isChecked()
        self._external_tools_cfg.animepahe_interval_hours = self._animepahe_interval_spin.value()
        self._external_tools_cfg.animepahe_last_url = self._animepahe_url_edit.text().strip()
        self._external_tools_cfg.animepahe_last_episodes = self._animepahe_episodes_edit.text().strip()
        self._external_tools_cfg.animepahe_last_quality = self._animepahe_quality_combo.currentText()
        self._external_tools_cfg.animepahe_last_lang = self._animepahe_lang_combo.currentText()

        # YouTube / yt-dlp settings (Phase 6)
        if hasattr(self, "_yt_enabled_cb"):
            self._current_youtube_config()
        self._external_tools_cfg.save()

        # 7. Collect Browser Integration settings
        self._browser_cfg.enabled = self._browser_enabled_cb.isChecked()
        self._browser_cfg.port = self._browser_port_spin.value()
        self._browser_cfg.intercept_all = self._browser_intercept_cb.isChecked()
        self._browser_cfg.intercept_torrent_files = self._browser_intercept_torrent_cb.isChecked()
        self._browser_cfg.intercept_magnet_links = self._browser_intercept_magnet_cb.isChecked()
        self._browser_cfg.min_file_size_kb = self._browser_min_size_spin.value()
        self._browser_cfg.skip_unknown_size_downloads = (
            self._browser_skip_unknown_size_cb.isChecked()
        )
        bypassed_text = self._browser_bypass_edit.text().strip()
        self._browser_cfg.bypassed_extensions = [
            ext.strip() for ext in bypassed_text.split(",") if ext.strip()
        ] if bypassed_text else []
        self._browser_cfg.save()
        if self._manager and hasattr(self._manager, "set_browser_config"):
            self._manager.set_browser_config(self._browser_cfg)

        # 7b. Collect Scheduler settings
        self._scheduler_cfg.enabled = self._scheduler_enable_cb.isChecked()
        self._scheduler_cfg.start_time = self._scheduler_start_time.time().toString("HH:mm")
        self._scheduler_cfg.end_time = self._scheduler_end_time.time().toString("HH:mm")
        self._scheduler_cfg.pause_when_ended = self._scheduler_pause_cb.isChecked()
        self._scheduler_cfg.days_of_week = [
            i for i, cb in enumerate(self._scheduler_day_cbs) if cb.isChecked()
        ]
        self._scheduler_cfg.save()
        if self._manager and hasattr(self._manager, "set_scheduler_config"):
            self._manager.set_scheduler_config(self._scheduler_cfg)

        # 8. Apply Views: segregated grouping and table columns. Last, because it writes
        # straight onto the live table rather than into a config object.
        if hasattr(self, "_apply_views_tab"):
            self._apply_views_tab()

        self.accept()

    @property
    def general_config(self) -> GeneralConfig:
        return self._general_cfg

    @property
    def torrent_config(self) -> TorrentConfig:
        return self._torrent_cfg

    @property
    def network_config(self) -> NetworkConfig:
        return self._network_cfg

    @property
    def security_config(self) -> SecurityConfig:
        return self._security_cfg

    @property
    def tor_config(self) -> TorConfig:
        return self._tor_cfg

    @property
    def external_tools_config(self) -> ExternalToolsConfig:
        return self._external_tools_cfg

    @property
    def browser_config(self) -> BrowserIntegrationConfig:
        return self._browser_cfg

    @property
    def scheduler_config(self) -> SchedulerConfig:
        return self._scheduler_cfg


class FirefoxInstallGuideDialog(QDialog):
    """Detailed modal guide explaining how to install and retain the Firefox add-on permanently."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Firefox Add-on: Permanent Installation Guide")
        self.resize(700, 520)
        self.setMinimumSize(560, 420)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        header_lbl = QLabel(
            "<h3 style='margin: 0; color: #ff9d00;'>🦊 Retaining My-IDM in Mozilla Firefox</h3>"
            "<p style='color: #8fa0b5; margin-top: 4px;'>"
            "By default, Firefox's <code>about:debugging</code> (<b>Load Temporary Add-on</b>) "
            "strictly removes all temporary extensions whenever Firefox closes or restarts. "
            "To keep My-IDM permanently active, choose one of the options below:"
            "</p>"
        )
        header_lbl.setTextFormat(Qt.TextFormat.RichText)
        header_lbl.setWordWrap(True)
        layout.addWidget(header_lbl)

        tabs = QTabWidget()

        # Tab 1: Firefox Developer Edition / ESR / Floorp / LibreWolf
        dev_tab = QWidget()
        dev_layout = QVBoxLayout(dev_tab)
        dev_text = (
            "<p style='line-height: 1.6;'>"
            "<b>Option A: Firefox Developer Edition / Nightly / ESR / LibreWolf / Floorp</b><br>"
            "<span style='color: #2ed573;'>Recommended for quick local installation without submitting to Mozilla.</span><br><br>"
            "1. In your browser address bar, navigate to: <a href='about:config' style='color: #ff9d00; text-decoration: underline;'><b>about:config</b></a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy URL]</span><br>"
            "2. Accept the risk warning, then search for: <a href='xpinstall.signatures.required' style='color: #00d2ff; text-decoration: underline;'><b>xpinstall.signatures.required</b></a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy]</span><br>"
            "3. Double-click to toggle its value to: <a href='false' style='color: #00d2ff; text-decoration: underline;'><b>false</b></a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy]</span>.<br>"
            "4. Click the <b>📦 Package Firefox Add-on (.xpi)</b> button below to generate <a href='copy:xpi_path' style='color: #ff9d00; text-decoration: underline;'><b>my-idm-firefox.xpi</b></a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy path]</span>.<br>"
            "5. Navigate to: <a href='about:addons' style='color: #ff9d00; text-decoration: underline;'><b>about:addons</b></a> "
            "&nbsp;<span style='color: #8fa0b5; font-size: 11px;'>[click to copy URL]</span> (or press <kbd>Ctrl+Shift+A</kbd>).<br>"
            "6. Click the <b>⚙️ Gear icon</b> at the top of the Add-ons manager and select <b>Install Add-on From File...</b><br>"
            "7. Choose the generated <code>my-idm-firefox.xpi</code> and click <b>Add</b>.<br><br>"
            "🎉 <b>Result:</b> The extension remains permanently installed across all browser updates and restarts!"
            "</p>"
        )
        dev_lbl = QLabel(dev_text)
        dev_lbl.setTextFormat(Qt.TextFormat.RichText)
        dev_lbl.setWordWrap(True)
        dev_lbl.setOpenExternalLinks(False)
        dev_lbl.linkActivated.connect(self._on_link_clicked)
        dev_layout.addWidget(dev_lbl)
        dev_layout.addStretch()
        tabs.addTab(dev_tab, "Developer / ESR / Forks (Instant)")

        # Tab 2: Standard Firefox Release (AMO Self-Distribution Signing)
        amo_tab = QWidget()
        amo_layout = QVBoxLayout(amo_tab)
        amo_text = (
            "<p style='line-height: 1.6;'>"
            "<b>Option B: Standard Firefox Release (Free Automated AMO Signing)</b><br>"
            "<span style='color: #2ed573;'>Works on 100% of standard official Firefox releases without modifying security flags.</span><br><br>"
            "Standard Firefox Release requires cryptographic signatures from Mozilla. Mozilla provides free automated self-distribution signing for personal use:<br><br>"
            "1. Click <b>📦 Package Firefox Add-on (.xpi)</b> below to create <code>my-idm-firefox.xpi</code>.<br>"
            "2. Visit Mozilla's Add-on Developer Hub: <a href='https://addons.mozilla.org/developers/addon/submit/distribution' style='color: #00d2ff;'><b>AMO Developer Hub</b></a> or view your builds directly at <a href='https://addons.mozilla.org/en-US/developers/addon/84510108e17d4c599bce/versions/6515778' style='color: #00d2ff;'><b>AMO Version 6515778</b></a>.<br>"
            "3. Log in with your free Mozilla Firefox Account.<br>"
            "4. Choose <b>'On your own' (Self-Distribution / Unlisted)</b>.<br>"
            "5. Upload <code>my-idm-firefox.xpi</code>. Mozilla's automated scanner approves and signs it in <b>1–2 minutes</b>.<br>"
            "6. Download your signed <code>.xpi</code> from the versions page and install it into Firefox.<br><br>"
            "🎉 <b>Result:</b> The signed add-on installs cleanly in standard Firefox and persists permanently!"
            "</p>"
        )
        amo_lbl = QLabel(amo_text)
        amo_lbl.setTextFormat(Qt.TextFormat.RichText)
        amo_lbl.setWordWrap(True)
        amo_lbl.linkActivated.connect(self._on_link_clicked)
        amo_layout.addWidget(amo_lbl)
        amo_layout.addStretch()
        tabs.addTab(amo_tab, "Standard Firefox Release (AMO Signing)")

        # Tab 3: Why Temporary Resets
        temp_tab = QWidget()
        temp_layout = QVBoxLayout(temp_tab)
        temp_text = (
            "<p style='line-height: 1.6;'>"
            "<b>Why does <code>about:debugging</code> disappear on restart?</b><br><br>"
            "Firefox's <i>'Load Temporary Add-on...'</i> feature is explicitly designed for developers to test code during an active session.<br><br>"
            "• Firefox purges temporary extensions upon restart to prevent unauthorized unsigned modifications from persisting without user knowledge.<br>"
            "• If you only need temporary downloads for a single session, <code>about:debugging</code> is sufficient.<br>"
            "• For permanent use across all restarts, follow <b>Option A</b> (if using Developer/ESR/Floorp) or <b>Option B</b> (for Standard Firefox)."
            "</p>"
        )
        temp_lbl = QLabel(temp_text)
        temp_lbl.setTextFormat(Qt.TextFormat.RichText)
        temp_lbl.setWordWrap(True)
        temp_layout.addWidget(temp_lbl)
        temp_layout.addStretch()
        tabs.addTab(temp_tab, "Why Temporary Resets")

        layout.addWidget(tabs)

        btn_row = QHBoxLayout()
        pkg_btn = QPushButton("📦 Package Firefox Add-on (.xpi)")
        pkg_btn.clicked.connect(self._on_package_clicked)
        btn_row.addWidget(pkg_btn)

        open_folder_btn = QPushButton("📁 Open .xpi Folder")
        open_folder_btn.clicked.connect(self._on_open_folder_clicked)
        btn_row.addWidget(open_folder_btn)

        self._copy_status_lbl = QLabel("")
        self._copy_status_lbl.setStyleSheet("color: #2ed573; font-weight: bold; margin-left: 8px;")
        btn_row.addWidget(self._copy_status_lbl, 1)

        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    def _copy_text(self, text: str, msg: str = ""):
        from PySide6.QtWidgets import QApplication, QToolTip
        from PySide6.QtGui import QCursor
        from PySide6.QtCore import QTimer, QRect
        import time
        cb = QApplication.clipboard()
        if cb:
            for _ in range(10):
                cb.setText(text)
                QApplication.processEvents()
                if cb.text() == text:
                    break
                time.sleep(0.015)
        QApplication.processEvents()
        try:
            QToolTip.showText(QCursor.pos(), f"✓ Copied: {text}", self, QRect(), 2500)
        except Exception:
            pass
        if hasattr(self, "_copy_status_lbl"):
            self._copy_status_lbl.setText(f"✓ Copied '{text}' to clipboard")
            QTimer.singleShot(3500, lambda: self._copy_status_lbl.setText(""))

    def _on_link_clicked(self, url: str):
        if url.startswith("http"):
            QDesktopServices.openUrl(QUrl(url))
        else:
            text = url
            if text.startswith("copy:"):
                text = text[len("copy:"):]
            if text == "xpi_path":
                from my_idm.extension_packager import get_default_extension_dir
                text = str(get_default_extension_dir() / "my-idm-firefox.xpi")
            self._copy_text(text)

    def _on_open_folder_clicked(self):
        from my_idm.extension_packager import get_default_extension_dir
        ext_dir = get_default_extension_dir()
        xpi_path = ext_dir / "my-idm-firefox.xpi"
        target = xpi_path if xpi_path.is_file() else ext_dir
        ok, msg = show_in_folder(target)
        if not ok:
            QMessageBox.warning(self, "Folder Error", msg)

    def _on_package_clicked(self):
        try:
            from my_idm.extension_packager import package_firefox_extension
            xpi_path = package_firefox_extension()
            res = QMessageBox.information(
                self,
                "Package Created",
                f"Firefox add-on package created successfully at:\n\n{xpi_path}\n\nWould you like to open the containing folder?",
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Ok,
                QMessageBox.StandardButton.Ok,
            )
            if res == QMessageBox.StandardButton.Open:
                ok, msg = show_in_folder(xpi_path)
                if not ok:
                    QMessageBox.warning(self, "Folder Error", msg)
        except Exception as e:
            QMessageBox.critical(self, "Packaging Error", f"Failed to package Firefox extension:\n{e}")


# -- Bandwidth Limit Dialog -----------------------------------------------

class BandwidthLimitDialog(QDialog):
    """Dialog to create or edit a bandwidth limit."""

    def __init__(self, parent=None, manager=None, db=None, limit=None):
        super().__init__(parent)
        self._manager = manager
        self._db = db or (manager._db if manager else None)
        self._limit = limit
        self.setWindowTitle("Edit Bandwidth Limit" if limit else "Add Bandwidth Limit")
        self.setMinimumWidth(420)
        self.setModal(True)

        self.queue_id = ""
        self.enabled = True
        self.limit_bytes = 10 * 1024 * 1024 * 1024  # default 10 GB
        self.limit_type = "monthly"
        self.warning_percent = 80

        self._setup_ui()
        if limit:
            self._populate_from_limit(limit)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        # Queue
        q_row = QHBoxLayout()
        q_lbl = QLabel("Queue:")
        q_lbl.setMinimumWidth(120)
        q_row.addWidget(q_lbl)
        self._queue_combo = QComboBox()
        self._queue_combo.addItem("Global (All Queues)", "")
        queues = []
        if self._manager and hasattr(self._manager, "get_queues"):
            try:
                queues = self._manager.get_queues()
            except Exception:
                pass
        elif self._db and hasattr(self._db, "get_queues"):
            try:
                queues = self._db.get_queues()
            except Exception:
                pass
        for q in queues:
            self._queue_combo.addItem(f"{q.name} ({q.id})", q.id)
        q_row.addWidget(self._queue_combo, 1)
        layout.addLayout(q_row)

        # Enabled
        en_row = QHBoxLayout()
        en_lbl = QLabel("Status:")
        en_lbl.setMinimumWidth(120)
        en_row.addWidget(en_lbl)
        self._enabled_cb = QCheckBox("Enable this limit")
        self._enabled_cb.setChecked(True)
        en_row.addWidget(self._enabled_cb, 1)
        layout.addLayout(en_row)

        # Limit value + unit
        lim_row = QHBoxLayout()
        lim_lbl = QLabel("Bandwidth Limit:")
        lim_lbl.setMinimumWidth(120)
        lim_row.addWidget(lim_lbl)
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(1, 1_000_000)
        self._limit_spin.setValue(10)
        lim_row.addWidget(self._limit_spin, 1)
        self._unit_combo = QComboBox()
        self._unit_combo.addItems(["GB", "MB"])
        lim_row.addWidget(self._unit_combo)
        layout.addLayout(lim_row)

        # Limit period
        type_row = QHBoxLayout()
        type_lbl = QLabel("Limit Period:")
        type_lbl.setMinimumWidth(120)
        type_row.addWidget(type_lbl)
        self._type_combo = QComboBox()
        self._type_combo.addItems(["Daily", "Weekly", "Monthly"])
        self._type_combo.setCurrentText("Monthly")
        type_row.addWidget(self._type_combo, 1)
        layout.addLayout(type_row)

        # Warning percentage
        warn_row = QHBoxLayout()
        warn_lbl = QLabel("Warning Threshold:")
        warn_lbl.setMinimumWidth(120)
        warn_row.addWidget(warn_lbl)
        self._warn_spin = QSpinBox()
        self._warn_spin.setRange(50, 99)
        self._warn_spin.setValue(80)
        self._warn_spin.setSuffix("%")
        warn_row.addWidget(self._warn_spin, 1)
        layout.addLayout(warn_row)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("Save" if self._limit else "Add")
        ok_btn.setObjectName("primaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_accept)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    def _populate_from_limit(self, limit: dict):
        self.queue_id = limit["queue_id"]
        self.enabled = bool(limit["enabled"])
        self.limit_bytes = limit["limit_bytes"]
        self.limit_type = limit["limit_type"]
        self.warning_percent = limit["warning_percent"]

        idx = self._queue_combo.findData(self.queue_id)
        if idx >= 0:
            self._queue_combo.setCurrentIndex(idx)

        self._enabled_cb.setChecked(self.enabled)

        # Set value and unit
        gb = 1024 * 1024 * 1024
        mb = 1024 * 1024
        if self.limit_bytes >= gb and self.limit_bytes % gb == 0:
            self._limit_spin.setValue(self.limit_bytes // gb)
            self._unit_combo.setCurrentText("GB")
        else:
            self._limit_spin.setValue(max(1, self.limit_bytes // mb))
            self._unit_combo.setCurrentText("MB")

        t_idx = {"daily": 0, "weekly": 1, "monthly": 2}.get(self.limit_type.lower(), 2)
        self._type_combo.setCurrentIndex(t_idx)
        self._warn_spin.setValue(self.warning_percent)

    def _on_accept(self):
        self.queue_id = self._queue_combo.currentData() or ""
        self.enabled = self._enabled_cb.isChecked()
        val = self._limit_spin.value()
        multiplier = 1024 * 1024 * 1024 if self._unit_combo.currentText() == "GB" else 1024 * 1024
        self.limit_bytes = val * multiplier
        self.limit_type = self._type_combo.currentText().lower()
        self.warning_percent = self._warn_spin.value()

        if self.limit_bytes <= 0:
            QMessageBox.warning(self, "Invalid Limit", "Bandwidth limit must be greater than 0.")
            return

        self.accept()

