"""Main application window for My-IDM."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QPoint, QSettings, QPointF, QTimer, QByteArray, QRect, QRectF, QEvent, QItemSelectionModel, Signal, QObject
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from my_idm.database import ALL_QUEUES, DEFAULT_QUEUE_ID, DEFAULT_QUEUE_NAME, Database, DownloadEntry
from my_idm.delegates import (
    DownloadNameDelegate,
    ProgressBarDelegate,
    QueueColumnDelegate,
    SavePathDelegate,
    SectionHeaderDelegate,
    get_section_select_all_btn_rect,
)
from my_idm.details_panel import DetailsPanel
from my_idm.dialogs import (
    AddDownloadDialog,
    AddQueueDialog,
    DeleteConfirmDialog,
    MoveDownloadDialog,
    RefreshAddressDialog,
    RenameDialog,
)
from my_idm.download_model import (
    DATE_SECTION_DEFS,
    DEFAULT_SEGREGATED_MODE,
    SEGREGATED_MODE_LABELS,
    SEGREGATED_MODES,
    TYPE_SECTION_DEFS,
    Col,
    DownloadTableModel,
)
from my_idm.header_view import FilterHeaderView
from my_idm.manager import DownloadManager
from my_idm.torrent_sources import local_torrent_paths_from_mime
from my_idm.resources import get_app_icon, get_app_logo_pixmap
from my_idm.network import NetworkConfig, is_vpn_adapter_name
from my_idm.network_dialog import NetworkSettingsDialog
from my_idm.security import SecurityConfig
from my_idm.config import TorConfig
from my_idm.security_dialog import SecuritySettingsDialog
from my_idm.settings_dialog import (
    TAB_BROWSER,
    TAB_EXTERNAL_TOOLS,
    TAB_GENERAL,
    TAB_SECURITY,
    TAB_TOR,
    TAB_TORRENT,
    TAB_VPN,
    TAB_BANDWIDTH,
    TAB_SCHEDULER,
    SettingsDialog,
)
from my_idm.external_tools import launch_animepahe_gui
from my_idm.styles import Colors
from my_idm.utils import create_color_swatch_icon
from my_idm import fonts

log = logging.getLogger(__name__)

# Visual order of the downloads-table columns, and the single source of truth for the startup
# layout, "Reset View", and the heal applied when restoring a UI state saved before columns
# were appended.
#
# This is a real arrangement rather than a designed one — taken from a profile that had every
# column visible and had been rearranged until it stopped being adjusted. Two things it does
# that a logical-order list would not: the **Queue** badge sits at slot 1, next to the row
# number, where the eye already is; and **Save Path** ("where is it going") sits with
# **Completed** and **Last Tried** instead of after them, so the three "what happened to it"
# columns are read together rather than split by insertion order.
#
# **Append new columns at the end.** Anything else drops the new column into the middle of a
# layout somebody has already arranged.
_DEFAULT_COLUMN_ORDER = (
    Col.QUEUE,              # "#"
    Col.QUEUE_NAME,         # "Queue"
    Col.NAME,
    Col.SIZE,
    Col.PROGRESS,
    Col.STATUS,
    Col.SPEED,
    Col.ETA,
    Col.SEEDS_PEERS,
    Col.ADDED,
    Col.SAVE_PATH,
    Col.COMPLETED,
    Col.LAST_TRIED,
    Col.SOURCE_DOMAIN,
    Col.FILE_NAME,
    Col.LAST_SEEDED,
    Col.SOURCE,
    Col.SEEDING_STARTED_AT,
)

#: The right-hand tail — the columns a stale-state restore has to re-pin at the end, because a
#: state saved before they existed cannot say where they went. Kept as its own name, and
#: asserted to be a suffix of ``_DEFAULT_COLUMN_ORDER``, because that is the rule the append
#: convention above depends on.
_DEFAULT_TAIL_COLUMNS = (
    Col.SOURCE_DOMAIN,
    Col.FILE_NAME,
    Col.LAST_SEEDED,
    Col.SOURCE,
    Col.SEEDING_STARTED_AT,
)

#: Default width per column, from the same arrangement. These add up to ~3080px against a
#: window that is rarely that wide, so a fresh profile scrolls sideways with all 18 columns
#: visible. That is deliberate and it is the user's own trade-off: a name column at 270px
#: truncated every long filename, and a file/folder name column at 220px truncated the other
#: half of them. Deliberately *not* fitted to a nominal window width.
_DEFAULT_COLUMN_WIDTHS = {
    Col.QUEUE: 50,
    Col.QUEUE_NAME: 30,
    Col.NAME: 412,
    Col.SIZE: 82,
    Col.PROGRESS: 214,
    Col.STATUS: 135,
    Col.SPEED: 166,
    Col.ETA: 80,
    Col.SEEDS_PEERS: 140,
    Col.ADDED: 123,
    Col.SAVE_PATH: 262,
    Col.COMPLETED: 130,
    Col.LAST_TRIED: 130,
    Col.SOURCE_DOMAIN: 187,
    Col.FILE_NAME: 546,
    Col.LAST_SEEDED: 131,
    Col.SOURCE: 110,
    Col.SEEDING_STARTED_AT: 173,
}

#: Columns hidden on a **fresh profile only**, when no header state has been saved yet.
#: Eighteen columns is too many to scan at a glance, and these are the ones a user reaches
#: for occasionally rather than watches. They stay listed in the Preferences column picker,
#: so nothing is unreachable - this is a starting arrangement, not a restriction.
#:
#: ``Col.ADDED`` is deliberately **not** here: it is the default sort column, and hiding it
#: hides the sort indicator with it, leaving the table looking unsorted.
#:
#: ``Col.QUEUE_NAME`` is deliberately **not** here either: which queue a download belongs to is
#: not incidental detail, and a queue is invisible in the list without this column.
#:
#: Deliberately not applied when a saved header state exists: a user who has arranged their
#: own columns must not have them overridden because a default changed. Change this tuple and
#: existing profiles are untouched; "Reset View" picks the new defaults up.
DEFAULT_HIDDEN_COLUMNS = (
    Col.SEEDING_STARTED_AT,
    Col.LAST_SEEDED,
    Col.SAVE_PATH,
    Col.SOURCE,
    Col.FILE_NAME,
)

#: Starting floor for the window width, replaced the moment the toolbar and the downloads list
#: have been measured - see ``MainWindow._fit_min_width_to_toolbar``. Only 600 is fixed: the
#: rows plus the details panel stacked under them genuinely do not compress further.
MIN_WINDOW_WIDTH = 640
MIN_WINDOW_HEIGHT = 600

#: Narrowest a downloads-table column may be dragged or set to. Qt's own floor comes from the
#: font and is 32px at the default UI font, which is wider than two of the default column
#: widths in ``_DEFAULT_COLUMN_WIDTHS``. 24 still fits a four-digit row number and keeps a
#: section grabbable; re-applied after every header restore by
#: ``MainWindow._apply_minimum_section_size``.
MIN_COLUMN_WIDTH = 24


def _apply_default_column_order(header) -> None:
    """Reorder *header* to ``_DEFAULT_COLUMN_ORDER``, preserving anything it does not name.

    Moves one column at a time in ascending slot order. Doing it incrementally
    is not enough: ``moveSection`` shifts everything between the source and the
    target, so placing a column that currently sits *left* of its target
    pushes an already-placed neighbour back out of position. Sweeping ascending
    fixes each slot permanently, because later moves only ever touch higher slots.

    A column the order does not mention — one added by a build newer than this
    tuple — keeps its relative position and lands at the end, which is the only
    safe answer: guessing a slot for it would put a new column in the middle of a
    layout somebody has already arranged.
    """
    known = [col for col in _DEFAULT_COLUMN_ORDER if col < header.count()]
    known_set = set(known)
    extra = [
        header.logicalIndex(v)
        for v in range(header.count())
        if header.logicalIndex(v) not in known_set
    ]
    for slot, col in enumerate(known + extra):
        visual = header.visualIndex(col)
        if visual != slot:
            header.moveSection(visual, slot)


def _apply_default_tail_order(header) -> None:
    """Pin the tail columns to the last slots, preserving the order of the rest.

    Moves one column at a time in ascending slot order. Doing it incrementally
    is not enough: ``moveSection`` shifts everything between the source and the
    target, so placing a tail column that currently sits *left* of its target
    pushes an already-placed neighbour back out of position. Sweeping ascending
    fixes each slot permanently, because later moves only ever touch higher slots.

    Used **only** for the stale-state heal, where the point is to place columns a
    saved state could not describe without disturbing the ones it could. Startup and
    "Reset View" want the whole arrangement and use
    ``_apply_default_column_order`` instead.
    """
    tail = list(_DEFAULT_TAIL_COLUMNS)
    tail_set = set(tail)
    non_tail = [
        header.logicalIndex(v)
        for v in range(header.count())
        if header.logicalIndex(v) not in tail_set
    ]
    for slot, col in enumerate(non_tail + tail):
        visual = header.visualIndex(col)
        if visual != slot:
            header.moveSection(visual, slot)


def _apply_default_column_widths(table) -> None:
    """Apply ``_DEFAULT_COLUMN_WIDTHS`` to *table*, one call instead of 18 lines.

    The widths used to be written out in three places — ``_setup_ui``,
    ``_on_reset_view`` and the stale-state heal — which is how they drifted apart.
    """
    for col, width in _DEFAULT_COLUMN_WIDTHS.items():
        table.setColumnWidth(col, width)

SPEED_LIMIT_PRESETS = [
    ("Unlimited", 0),
    ("1 kbps", 1 * 1024),
    ("2 kbps", 2 * 1024),
    ("5 kbps", 5 * 1024),
    ("10 kbps", 10 * 1024),
    ("50 kbps", 50 * 1024),
    ("100 kbps", 100 * 1024),
    ("200 kbps", 200 * 1024),
    ("500 kbps", 500 * 1024),
    ("1 mbps", 1 * 1024 * 1024),
    ("2 mbps", 2 * 1024 * 1024),
    ("5 mbps", 5 * 1024 * 1024),
    ("10 mbps", 10 * 1024 * 1024),
    ("100 mbps", 100 * 1024 * 1024),
]


def _create_play_icon(size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#4ade80"))
    p.setPen(Qt.PenStyle.NoPen)
    triangle = QPolygonF([
        QPointF(size * 0.25, size * 0.18),
        QPointF(size * 0.82, size * 0.5),
        QPointF(size * 0.25, size * 0.82),
    ])
    p.drawPolygon(triangle)
    p.end()
    return QIcon(pix)


def _create_pause_icon(size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#38bdf8"))
    p.setPen(Qt.PenStyle.NoPen)
    bar_w = size * 0.22
    bar_h = size * 0.64
    y = size * 0.18
    p.drawRoundedRect(size * 0.2, y, bar_w, bar_h, 2, 2)
    p.drawRoundedRect(size * 0.58, y, bar_w, bar_h, 2, 2)
    p.end()
    return QIcon(pix)


def _create_stop_icon(size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#ef4444"))
    p.setPen(Qt.PenStyle.NoPen)
    margin = size * 0.2
    side = size - 2 * margin
    p.drawRoundedRect(margin, margin, side, side, 3, 3)
    p.end()
    return QIcon(pix)


def _create_emoji_icon(emoji: str, size: int = 32) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = fonts.emoji_font(12)
    font.setPixelSize(int(size * 0.65))
    p.setFont(font)
    p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, emoji)
    p.end()
    return QIcon(pix)


def _create_force_start_icon(size: int = 32) -> QIcon:
    """Create a play triangle icon containing an 'F' (Force Start)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setBrush(QColor("#4ade80"))
    p.setPen(Qt.PenStyle.NoPen)
    triangle = QPolygonF([
        QPointF(size * 0.16, size * 0.16),
        QPointF(size * 0.88, size * 0.5),
        QPointF(size * 0.16, size * 0.84),
    ])
    p.drawPolygon(triangle)

    # Draw bold 'F' inside the play button
    font = p.font()
    font.setBold(True)
    font.setPixelSize(int(size * 0.42))
    p.setFont(font)
    p.setPen(QColor("#0d1117"))
    text_rect = QRectF(size * 0.20, size * 0.22, size * 0.44, size * 0.56)
    p.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "F")
    p.end()
    return QIcon(pix)


def _create_details_panel_icon(size: int = 32) -> QIcon:
    """Create a sleek icon showing window layout with bottom details panel highlighted."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Window outer outline
    pen = QPen(QColor("#8b949e"), max(1.5, size * 0.065))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(size * 0.12, size * 0.12, size * 0.76, size * 0.76)
    p.drawRoundedRect(rect, 3, 3)

    # Divider line
    p.setPen(QPen(QColor("#8b949e"), max(1.2, size * 0.055)))
    p.drawLine(QPointF(size * 0.12, size * 0.54), QPointF(size * 0.88, size * 0.54))

    # Highlighted bottom details panel
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#58a6ff"))
    bot_rect = QRectF(size * 0.16, size * 0.58, size * 0.68, size * 0.26)
    p.drawRoundedRect(bot_rect, 2, 2)

    p.end()
    return QIcon(pix)


def _create_pause_all_icon(size: int = 32) -> QIcon:
    """Pause icon (two bars) with a small ≡ badge in the bottom-right to denote 'all'."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#38bdf8"))
    p.setPen(Qt.PenStyle.NoPen)
    bar_w = size * 0.18
    bar_h = size * 0.54
    y = size * 0.10
    p.drawRoundedRect(size * 0.10, y, bar_w, bar_h, 2, 2)
    p.drawRoundedRect(size * 0.36, y, bar_w, bar_h, 2, 2)
    # Badge: three small yellow horizontal lines in bottom-right
    lw = max(1.5, size * 0.065)
    p.setPen(QPen(QColor("#facc15"), lw, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    bx1 = size * 0.56
    bx2 = size * 0.88
    for by in (size * 0.66, size * 0.76, size * 0.86):
        p.drawLine(QPointF(bx1, by), QPointF(bx2, by))
    p.end()
    return QIcon(pix)


def _create_resume_all_icon(size: int = 32) -> QIcon:
    """Play icon (triangle) with a small ≡ badge in the bottom-right to denote 'all'."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#4ade80"))
    p.setPen(Qt.PenStyle.NoPen)
    triangle = QPolygonF([
        QPointF(size * 0.12, size * 0.10),
        QPointF(size * 0.64, size * 0.38),
        QPointF(size * 0.12, size * 0.66),
    ])
    p.drawPolygon(triangle)
    # Badge: three small yellow horizontal lines in bottom-right
    lw = max(1.5, size * 0.065)
    p.setPen(QPen(QColor("#facc15"), lw, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    bx1 = size * 0.56
    bx2 = size * 0.88
    for by in (size * 0.66, size * 0.76, size * 0.86):
        p.drawLine(QPointF(bx1, by), QPointF(bx2, by))
    p.end()
    return QIcon(pix)


def _create_pause_all_seeding_icon(size: int = 32) -> QIcon:
    """Pause icon (two bars) with a clearly visible sprout (🌱) badge in the bottom-right."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Pause bars in cyan
    p.setBrush(QColor("#38bdf8"))
    p.setPen(Qt.PenStyle.NoPen)
    bar_w = size * 0.18
    bar_h = size * 0.54
    y = size * 0.10
    p.drawRoundedRect(size * 0.10, y, bar_w, bar_h, 2, 2)
    p.drawRoundedRect(size * 0.36, y, bar_w, bar_h, 2, 2)

    # Seedling sprout badge in bottom-right
    # Stem: green vertical stalk
    p.setPen(QPen(QColor("#22c55e"), max(1.6, size * 0.08), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(size * 0.74, size * 0.92), QPointF(size * 0.74, size * 0.60))

    # Left leaf
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#4ade80"))
    path_l = QPainterPath()
    path_l.moveTo(size * 0.74, size * 0.68)
    path_l.quadTo(size * 0.52, size * 0.62, size * 0.52, size * 0.50)
    path_l.quadTo(size * 0.66, size * 0.50, size * 0.74, size * 0.68)
    p.drawPath(path_l)

    # Right leaf (slightly brighter)
    p.setBrush(QColor("#86efac"))
    path_r = QPainterPath()
    path_r.moveTo(size * 0.74, size * 0.64)
    path_r.quadTo(size * 0.96, size * 0.58, size * 0.96, size * 0.46)
    path_r.quadTo(size * 0.82, size * 0.46, size * 0.74, size * 0.64)
    p.drawPath(path_r)

    p.end()
    return QIcon(pix)


class _RightClickGuard(QObject):
    """Event filter that makes a ``QMenu`` ignore the right mouse button entirely.

    ``QMenu`` treats a right-button press followed by a release as an ordinary activation
    of whatever item is under the cursor. On the tray menu that is genuinely dangerous:
    the bottom two rows are **Restart My-IDM** and **Exit My-IDM**, so a reflexive
    right-click - the gesture people reach for when they miss a left-click - shut the
    application down, mid-download, with no confirmation.

    Swallowing the button events means a right-click can only ever dismiss the menu. Left,
    middle and back pass through, so the menu stays usable.

    Only the *button* is filtered. Press, release and double-click must all be swallowed or
    the press/release pair still reaches ``QMenu``'s activation logic, so a partial guard
    would look installed and change nothing. ``QMenu`` has no ``viewport()`` in Qt 6 - it
    paints its own items - so the menu itself is the only object that needs the filter.
    """

    _BLOCKED = (
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
    )

    def eventFilter(self, watched, event):
        if (
            event.type() in self._BLOCKED
            and event.button() == Qt.MouseButton.RightButton
        ):
            return True  # consume: QMenu never sees it, so nothing activates
        return super().eventFilter(watched, event)


class MainWindow(QMainWindow):
    """The main My-IDM window."""

    _sig_show_tray_notification = Signal(str, str, int)

    def __init__(
        self,
        manager: DownloadManager,
        show_exit_splash: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._manager = manager
        self._show_exit_splash = show_exit_splash
        self._tray_icon: Optional[QSystemTrayIcon] = None
        self._tray_act_capture: Optional[QAction] = None
        self._tray_act_clipboard: Optional[QAction] = None
        # Created in _setup_capture, which always runs; held here so the clipboard
        # subscription and the OS hotkey registration cannot be garbage collected.
        self._clipboard_monitor = None
        self._hotkey = None
        self._force_exit: bool = False
        self._close_to_tray_notified: bool = False
        self._completed_notified: set[str] = set()
        # The live statistics popup, if open. Held so a second toolbar click raises it
        # instead of building another, and dropped when it closes.
        self._stats_dialog = None

        self.setWindowTitle("My-IDM — Download Manager")
        # The width half is a placeholder: `_fit_min_width_to_toolbar` measures the toolbar
        # and the downloads list at the end of `_setup_toolbar` and again on first show, and
        # raises this to whichever needs more. A fixed 1100px outlived the queue switcher and
        # the Statistics button by ~230px.
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.resize(1400, 750)
        self.setWindowIcon(get_app_icon())

        # .torrent files may be dropped anywhere in the window. `setAcceptDrops` on the window
        # is what makes that one line work over the table, the details panel, the toolbar and the
        # status bar: Qt hands a drag to the widget under the cursor and propagates it up the
        # parent chain until something accepts, and no child here accepts, so it reaches us.
        # The two regions this deliberately does not cover are documented on the handlers.
        # .torrent drag-and-drop is armed in showEvent, not here — see the comment there.
        # Model
        self._model = DownloadTableModel(self)

        # Apply the persisted theme **before** anything is built. `main.py` sets the dark
        # sheet as a default before the window exists; doing it here means a user who chose
        # Light gets no flash of dark on startup, and it covers entry points that do not go
        # through `main.py`.
        self._apply_persisted_theme()

        self._setup_ui()
        self._setup_actions()
        self._setup_toolbar()
        self._setup_menubar()
        self._setup_statusbar()
        self._setup_system_tray()
        self._setup_capture()
        self._connect_signals()

        # Restore window geometry, location, column lengths, and splitter from DB
        self._restore_ui_state_from_db()

        # Load existing downloads from DB
        self._load_history()

        # Apply the persisted queue scope and populate the switcher. After `_load_history`,
        # not before: narrowing the scope is a filter, and a filtered-out download must never
        # flash on screen during startup.
        self._init_queue_scope()

        # Check bandwidth limits initially to populate warning badge if needed
        self._manager.check_all_bandwidth_limits()

    # -- UI setup ------------------------------------------------------------

    def _setup_ui(self):
        # Table view
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._table.setSortingEnabled(True)
        self._last_sort_section = Col.ADDED
        self._table.sortByColumn(
            Col.ADDED, Qt.SortOrder.DescendingOrder
        )
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)
        self._table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._table.setMouseTracking(True)
        self._table.viewport().setMouseTracking(True)

        self._section_delegate = SectionHeaderDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.QUEUE, self._section_delegate
        )

        self._name_delegate = DownloadNameDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.NAME, self._name_delegate
        )

        # Progress bar delegate
        self._progress_delegate = ProgressBarDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.PROGRESS, self._progress_delegate
        )

        # Save path delegate with intelligent shortening
        self._save_path_delegate = SavePathDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.SAVE_PATH, self._save_path_delegate
        )

        self._file_name_delegate = DownloadNameDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.FILE_NAME, self._file_name_delegate
        )

        # Queue column: colour swatch beside the name, collapsing to the swatch when narrow.
        self._queue_delegate = QueueColumnDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.QUEUE_NAME, self._queue_delegate
        )

        # Filterable and movable column header with sort indicators
        self._header_view = FilterHeaderView(self._table)
        self._table.setHorizontalHeader(self._header_view)
        header = self._header_view
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setCascadingSectionResizes(False)
        header.setDefaultSectionSize(110)
        self._apply_minimum_section_size()
        header.setSectionsMovable(True)
        header.setFirstSectionMovable(True)
        header.sectionMoved.connect(self._on_section_moved)
        header.filter_requested.connect(self._on_header_filter_requested)
        header.sectionClicked.connect(self._on_header_section_clicked)

        self._model.set_tor_config(self._manager.tor_config)
        # The model needs to know whether a Tor proxy is live so a per-download
        # route is not shown as active when Tor is down. It reads the manager's
        # *cached* flag, which is refreshed off the GUI thread.
        self._model.set_tor_availability_provider(self._manager.tor_available)
        self._manager.tor_availability_changed.connect(self._on_tor_availability_changed)
        self._tor_availability_timer = QTimer(self)
        self._tor_availability_timer.setInterval(4000)
        self._tor_availability_timer.timeout.connect(self._manager.refresh_tor_availability)
        self._tor_availability_timer.start()
        self._manager.refresh_tor_availability()
        self._table.clicked.connect(self._on_table_clicked)
        self._table.doubleClicked.connect(self._on_table_double_clicked)
        self._model.modelReset.connect(self._apply_table_spans)
        self._model.layoutChanged.connect(self._apply_table_spans)

        # Segregated view: disabled by default, state and mode persisted in db
        self._segregated_view_enabled = bool(self._manager.db.get_ui_state("segregated_view_enabled", False))
        self._segregated_view_mode = str(self._manager.db.get_ui_state("segregated_view_mode", "status"))
        # Use the shared mode list, not a local pair: the old two-value whitelist left here
        # when the "type" mode was added, so choosing File Type and restarting silently
        # grouped the table by Status instead.
        if self._segregated_view_mode not in SEGREGATED_MODES:
            self._segregated_view_mode = DEFAULT_SEGREGATED_MODE

        all_sec_ids = (
            "active", "seeding", "inactive",
            "date_today", "date_yesterday",
            "date_last_7_days", "date_this_week",
            "date_last_30_days", "date_this_month",
            "date_older",
        )
        for sec_id in all_sec_ids:
            if self._manager.db.get_ui_state(f"segregated_{sec_id}_collapsed", False):
                canonical_sec_id = sec_id
                if sec_id == "date_this_week":
                    canonical_sec_id = "date_last_7_days"
                elif sec_id == "date_this_month":
                    canonical_sec_id = "date_last_30_days"
                self._model.set_section_collapsed(canonical_sec_id, True)
        self._model.set_segregated_view(self._segregated_view_enabled, mode=self._segregated_view_mode)
        self._apply_table_spans()

        # Default column order (see _DEFAULT_COLUMN_ORDER).
        _apply_default_column_order(header)

        # Default column widths (see _DEFAULT_COLUMN_WIDTHS).
        _apply_default_column_widths(self._table)

        # Row height
        self._table.verticalHeader().setDefaultSectionSize(36)

        # Context menu
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._table.customContextMenuRequested.connect(self._show_context_menu)

        # Splitter with download table on top and details panel on bottom
        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._splitter.addWidget(self._table)
        self._details_panel = DetailsPanel(self._manager, self)
        self._details_panel.setMinimumHeight(140)
        if hasattr(self._details_panel, "browser_container_hwnd"):
            try:
                self._manager.set_browser_container_hwnd(self._details_panel.browser_container_hwnd)
            except Exception:
                pass
        self._details_panel.browser_tab_requested.connect(self._on_browser_tab_requested)
        self._details_panel.manage_queues_requested.connect(self._on_manage_queues)
        self._splitter.addWidget(self._details_panel)
        self._splitter.setChildrenCollapsible(False)
        self._details_height: int = 250
        self._splitter.setSizes([450, self._details_height])
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

        self.setCentralWidget(self._splitter)

    def _on_browser_tab_requested(self):
        """Ensure bottom panel is visible and expanded when an external browser session opens."""
        # Avoid expanding details panel while in tray (window hidden)
        if not self.isVisible() or self.isMinimized():
            return
        if hasattr(self, "_act_details") and not self._act_details.isChecked():
            self._act_details.setChecked(True)
            self._on_details_toggle(True)
        sizes = self._splitter.sizes()
        if len(sizes) == 2 and sizes[1] < 340:
            total = sum(sizes)
            top_h = max(100, total - 360)
            bot_h = total - top_h
            self._splitter.setSizes([top_h, bot_h])

    def _on_splitter_moved(self, pos: int, index: int):
        """Track user-adjusted details panel height in real time."""
        if self._details_panel.isVisible():
            sizes = self._splitter.sizes()
            if len(sizes) == 2 and sizes[1] >= 50:
                self._details_height = sizes[1]

    def _setup_actions(self):
        """Create all QActions."""
        self._act_add = QAction(_create_emoji_icon("➕"), "Add Download", self)
        self._act_add.setShortcut(QKeySequence("Ctrl+N"))
        self._act_add.setToolTip("Add URL, Magnet Link, or .torrent file (Ctrl+N)")
        self._act_add.triggered.connect(self._on_add)

        self._act_add_torrent = QAction(_create_emoji_icon("📦"), "Add Torrent File…", self)
        self._act_add_torrent.setShortcut(QKeySequence("Ctrl+T"))
        self._act_add_torrent.setToolTip("Add .torrent file (Ctrl+T)")
        self._act_add_torrent.triggered.connect(self._on_add_torrent)

        # Standard style icon rather than an emoji, so the Tools menu keeps its
        # uniform indentation (every menu action is expected to carry an icon).
        _yt_style_icon = (
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            if QApplication.style() is not None
            else _create_emoji_icon("🎬")
        )
        self._act_youtube = QAction(_yt_style_icon, "Download YouTube Video…", self)
        self._act_youtube.setShortcut(QKeySequence("Ctrl+Y"))
        self._act_youtube.setToolTip("Analyse a YouTube link and choose a quality (Ctrl+Y)")
        self._act_youtube.triggered.connect(self._on_add_youtube)

        self._act_resume = QAction(_create_play_icon(), "Resume", self)
        self._act_resume.setShortcut(QKeySequence("Ctrl+R"))
        self._act_resume.setToolTip("Resume selected downloads (Ctrl+R)")
        self._act_resume.triggered.connect(self._on_resume)

        self._act_force_start = QAction(_create_force_start_icon(), "Force Start", self)
        self._act_force_start.setToolTip("Force start selected download(s) immediately (overrides off-peak schedule)")
        self._act_force_start.triggered.connect(self._on_force_start)

        self._act_pause = QAction(_create_pause_icon(), "Pause", self)
        self._act_pause.setShortcut(QKeySequence("Space"))
        self._act_pause.setToolTip("Pause selected downloads (Space)")
        self._act_pause.triggered.connect(self._on_pause)

        self._act_stop = QAction(_create_stop_icon(), "Stop", self)
        self._act_stop.setToolTip("Stop selected downloads permanently until manually resumed")
        self._act_stop.triggered.connect(self._on_stop)

        self._act_start_seeding = QAction(_create_emoji_icon("🌱"), "Start Seeding", self)
        self._act_start_seeding.setToolTip("Start or resume seeding for completed torrents")
        self._act_start_seeding.triggered.connect(self._on_start_seeding)

        self._act_pause_all = QAction(_create_pause_all_icon(), "Pause All", self)
        self._act_pause_all.setToolTip("Pause all active and queued downloads")
        self._act_pause_all.triggered.connect(self._on_pause_all_downloads)

        self._act_resume_all = QAction(_create_resume_all_icon(), "Resume All", self)
        self._act_resume_all.setToolTip("Resume all paused and queued downloads")
        self._act_resume_all.triggered.connect(self._on_resume_all_downloads)

        self._act_stop_all_seeding = QAction(_create_pause_all_seeding_icon(), "Pause All Seeding", self)
        self._act_stop_all_seeding.setToolTip("Pause all active seeding torrents")
        self._act_stop_all_seeding.triggered.connect(self._on_stop_all_seeding)

        self._act_copy_url = QAction(_create_emoji_icon("📋"), "Copy URL / Magnet", self)
        self._act_copy_url.setShortcut(QKeySequence("Ctrl+C"))
        self._act_copy_url.setToolTip("Copy download URL or Magnet link to clipboard (Ctrl+C)")
        self._act_copy_url.triggered.connect(self._on_copy_url)

        self._act_refresh_address = QAction(_create_emoji_icon("🔗"), "Refresh Address…", self)
        self._act_refresh_address.setToolTip("Edit or refresh expired download URL without losing progress")
        self._act_refresh_address.triggered.connect(self._on_refresh_address)

        self._act_rename = QAction(_create_emoji_icon("✏️"), "Rename…", self)
        self._act_rename.setShortcut(QKeySequence("F2"))
        self._act_rename.setToolTip("Rename downloaded file or folder (F2)")
        self._act_rename.triggered.connect(self._on_rename)

        self._act_delete = QAction(_create_emoji_icon("🗑"), "Delete", self)
        self._act_delete.setShortcut(QKeySequence("Delete"))
        self._act_delete.setToolTip("Delete selected downloads")
        self._act_delete.triggered.connect(self._on_delete)

        self._act_delete_file = QAction(_create_emoji_icon("🗑"), "Delete File", self)
        self._act_delete_file.setToolTip("Delete downloaded file from disk (move to Trash), keeping entry paused at 0%")
        self._act_delete_file.triggered.connect(self._on_delete_file)

        self._act_move = QAction(_create_emoji_icon("📂"), "Move…", self)
        self._act_move.setToolTip("Move download to another directory")
        self._act_move.triggered.connect(self._on_move)

        self._act_recheck = QAction(_create_emoji_icon("🔄"), "Recheck", self)
        self._act_recheck.setToolTip(
            "Verify existing files on disk. Also the way back from \"File not found\": "
            "restore the file and recheck to confirm it complete."
        )
        self._act_recheck.triggered.connect(self._on_recheck)

        self._act_open_file = QAction(_create_emoji_icon("📄"), "Open File", self)
        self._act_open_file.setShortcut(QKeySequence("Return"))
        self._act_open_file.setToolTip("Open the downloaded file")
        self._act_open_file.triggered.connect(self._on_open_file)

        self._act_open_folder = QAction(_create_emoji_icon("📁"), "Open Folder", self)
        self._act_open_folder.setShortcut(QKeySequence("Ctrl+O"))
        self._act_open_folder.setToolTip("Open containing folder (Ctrl+O)")
        self._act_open_folder.triggered.connect(self._on_open_folder)

        self._act_load_backlog = QAction(_create_emoji_icon("📋"), "Load Backlog…", self)
        self._act_load_backlog.setShortcut(QKeySequence("Ctrl+L"))
        self._act_load_backlog.setToolTip("Load URLs from a backlog file")
        self._act_load_backlog.triggered.connect(self._on_load_backlog)

        self._act_move_up = QAction(_create_emoji_icon("⬆"), "Move Up in Queue", self)
        self._act_move_up.setToolTip("Move selected download up in queue order")
        self._act_move_up.triggered.connect(self._on_move_queue_up)

        self._act_move_down = QAction(_create_emoji_icon("⬇"), "Move Down in Queue", self)
        self._act_move_down.setToolTip("Move selected download down in queue order")
        self._act_move_down.triggered.connect(self._on_move_queue_down)

        self._act_stats = QAction(_create_emoji_icon("📊"), "Statistics…", self)
        self._act_stats.setToolTip(
            "Statistics: download and upload totals for today, this week, this month and this year"
        )
        self._act_stats.triggered.connect(self._on_show_statistics)

        self._act_preferences = QAction(_create_emoji_icon("⚙"), "Preferences…", self)
        self._act_preferences.setShortcut(QKeySequence("Ctrl+,"))
        self._act_preferences.setToolTip(
            "Configure default download folder, performance, network, and security (Ctrl+,)"
        )
        self._act_preferences.triggered.connect(
            lambda: self._on_open_preferences(TAB_GENERAL)
        )

        self._act_torrent_settings = QAction(_create_emoji_icon("🧲"), "BitTorrent Settings…", self)
        self._act_torrent_settings.setToolTip(
            "Configure BitTorrent seeding behavior, speed limits, and metadata timeout"
        )
        self._act_torrent_settings.triggered.connect(
            self._on_open_torrent_settings
        )

        self._act_network_settings = QAction(_create_emoji_icon("🌐"), "VPN & Network Settings…", self)
        self._act_network_settings.setToolTip(
            "Configure VPN adapter binding, Kill Switch, and Proxy"
        )
        self._act_network_settings.triggered.connect(
            self._on_open_network_settings
        )

        self._act_security_settings = QAction(_create_emoji_icon("🛡"), "Antivirus & Security Settings…", self)
        self._act_security_settings.setToolTip(
            "Configure pre-download URL inspection and post-download antivirus scanning"
        )
        self._act_security_settings.triggered.connect(
            self._on_open_security_settings
        )

        self._act_bandwidth_settings = QAction(_create_emoji_icon("📊"), "Bandwidth Limit Settings…", self)
        self._act_bandwidth_settings.setToolTip(
            "Configure daily, weekly, or monthly bandwidth limits per queue or globally"
        )
        self._act_bandwidth_settings.triggered.connect(
            self._on_open_bandwidth_settings
        )

        self._act_scheduler_settings = QAction(_create_emoji_icon("⏱️"), "Scheduler Settings…", self)
        self._act_scheduler_settings.setToolTip(
            "Configure off-peak download hours and schedule"
        )
        self._act_scheduler_settings.triggered.connect(
            self._on_open_scheduler_settings
        )

        self._act_scan_antivirus = QAction(_create_emoji_icon("🛡"), "Scan with Antivirus", self)
        self._act_scan_antivirus.setToolTip("Scan the downloaded file with antivirus")
        self._act_scan_antivirus.triggered.connect(self._on_scan_selected_file)

        self._act_toggle_details = QAction(_create_details_panel_icon(), "Details Panel", self)
        self._act_toggle_details.setCheckable(True)
        self._act_toggle_details.setChecked(True)
        self._act_toggle_details.setShortcut(QKeySequence("F4"))
        self._act_toggle_details.setToolTip("Hide bottom download details panel (F4)")
        self._act_toggle_details.toggled.connect(self._on_toggle_details)
        self._details_panel.close_requested.connect(
            lambda: self._act_toggle_details.setChecked(False)
        )

        self._act_reset_view = QAction(_create_emoji_icon("🔄"), "Reset View", self)
        self._act_reset_view.setToolTip("Reset column visibility, filters, sorting, and column widths to defaults")
        self._act_reset_view.triggered.connect(self._on_reset_view)

        self._act_tor = QAction(_create_emoji_icon("🧅"), "Tor: OFF", self)
        self._act_tor.setCheckable(False)
        self._act_tor.setToolTip("Toggle Tor network privacy routing (SOCKS5 proxy)")
        self._act_tor.triggered.connect(
            lambda: self._on_toggle_tor(not self._manager.tor_config.enabled)
        )

    def _setup_toolbar(self):
        toolbar = QToolBar("Main Toolbar")
        self._toolbar = toolbar
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        toolbar.addAction(self._act_add)
        toolbar.addSeparator()
        toolbar.addAction(self._act_resume)
        toolbar.addAction(self._act_force_start)
        toolbar.addAction(self._act_pause)
        toolbar.addAction(self._act_stop)
        toolbar.addAction(self._act_start_seeding)
        toolbar.addSeparator()

        # Resume All + Pause All + Pause All Seeding
        toolbar.addAction(self._act_resume_all)
        toolbar.addAction(self._act_pause_all)
        toolbar.addAction(self._act_stop_all_seeding)
        toolbar.addSeparator()
        toolbar.addAction(self._act_delete)
        toolbar.addAction(self._act_move)
        toolbar.addAction(self._act_recheck)
        toolbar.addSeparator()

        # Tor toolbar control with embedded progress bar
        self._tor_toolbar_container = QWidget()
        self._tor_toolbar_container.setObjectName("tor_toolbar_container")
        self._tor_toolbar_container.setStyleSheet("background: transparent;")
        self._tor_toolbar_container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        tor_tb_layout = QVBoxLayout(self._tor_toolbar_container)
        tor_tb_layout.setContentsMargins(0, 0, 0, 0)
        tor_tb_layout.setSpacing(1)

        self._tor_toolbar_btn = QToolButton()
        self._tor_toolbar_btn.setDefaultAction(self._act_tor)
        self._tor_toolbar_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tor_tb_layout.addWidget(self._tor_toolbar_btn)

        self._tor_toolbar_progress = QProgressBar()
        self._tor_toolbar_progress.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._tor_toolbar_progress.setFixedHeight(4)
        self._tor_toolbar_progress.setTextVisible(False)
        self._tor_toolbar_progress.setVisible(False)
        self._tor_toolbar_progress.setStyleSheet("""
            QProgressBar {
                background: #21252b;
                border: none;
                border-radius: 2px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #50fa7b, stop:1 #8be9fd);
                border-radius: 2px;
            }
        """)
        tor_tb_layout.addWidget(self._tor_toolbar_progress)

        toolbar.addWidget(self._tor_toolbar_container)
        toolbar.addSeparator()

        # Quick search over the visible downloads. It is the only expanding item on the
        # toolbar, so it absorbs whatever width is left after the action groups instead of
        # leaving dead space beside it - it used to sit behind a 1px expanding spacer *and*
        # be capped at 320px, which wasted exactly the room the search field could use.
        self._search_edit = QLineEdit()
        self._search_edit.setObjectName("toolbar_search")
        self._search_edit.setPlaceholderText("Search downloads…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setToolTip(
            "Filter downloads by name, original name, URL, or domain"
        )
        self._search_edit.setMinimumWidth(160)
        self._search_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._search_edit.setFixedHeight(26)
        self._search_edit.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self._search_edit)

        # Separate the search field from the stats button, and stats from preferences
        toolbar.addSeparator()
        toolbar.addAction(self._act_stats)
        toolbar.addSeparator()
        toolbar.addAction(self._act_preferences)

        # Show only icons without text for playback, action, and stats buttons
        for act in (
            self._act_resume,
            self._act_force_start,
            self._act_pause,
            self._act_stop,
            self._act_start_seeding,
            self._act_resume_all,
            self._act_pause_all,
            self._act_stop_all_seeding,
            self._act_delete,
            self._act_move,
            self._act_recheck,
            self._act_stats,
        ):
            btn = toolbar.widgetForAction(act)
            if isinstance(btn, QToolButton):
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        # Last, because it measures the finished toolbar.
        self._fit_min_width_to_toolbar()

        self.addToolBar(toolbar)

    def _fit_min_width_to_toolbar(self):
        """Set the window's minimum width to what its contents actually need.

        Two things set that floor, and the wider of the two wins:

        - the toolbar's ``sizeHint``, because a ``QToolBar`` narrower than its contents folds
          the remainder into a ``>>`` button, which hides download controls behind a second
          click;
        - the downloads list's own minimum, because a window narrower than that clips the
          columns instead of scrolling them.

        Measured rather than hard-coded, because both numbers move: the row's width with the
        font, the scale factor and which actions are on it, the list's with its columns. The
        previous fixed 1100px floor was measured against a toolbar carrying a queue switcher
        and a Statistics button, so it outlived both of them by ~230px.

        Capped at the screen's available width: on a display too small for the full row, a
        minimum the window cannot be fitted to would be worse than the overflow button the
        toolbar bound is meant to remove.

        Only ever raises. A narrower toolbar later - a shorter Tor label, a shorter locale -
        must not shrink a window the user has already opened.
        """
        central = self.centralWidget()
        needed = max(
            self._toolbar.sizeHint().width(),
            central.minimumSizeHint().width() if central is not None else 0,
        )
        if needed <= 0:
            return
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry().width() if screen else needed
        target = min(needed, available)
        if target > self.minimumWidth():
            self.setMinimumWidth(target)

    def _setup_menubar(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(self._act_add)
        file_menu.addAction(self._act_add_torrent)
        file_menu.addAction(self._act_load_backlog)
        file_menu.addSeparator()

        exit_act = QAction(_create_emoji_icon("🚪"), "Exit", self)
        exit_act.setShortcut(QKeySequence("Ctrl+Q"))
        exit_act.triggered.connect(self._exit_app)
        file_menu.addAction(exit_act)

        # Edit menu
        edit_menu = menubar.addMenu("&Edit")
        edit_menu.addAction(self._act_resume)
        edit_menu.addAction(self._act_force_start)
        edit_menu.addAction(self._act_pause)
        edit_menu.addAction(self._act_resume_all)
        edit_menu.addAction(self._act_pause_all)
        edit_menu.addAction(self._act_stop)
        edit_menu.addAction(self._act_start_seeding)
        edit_menu.addAction(self._act_stop_all_seeding)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_copy_url)
        edit_menu.addAction(self._act_refresh_address)
        edit_menu.addSeparator()
        # The file operations, in the same order the row context menu uses them: rename it,
        # move it, look at it, destroy it. Rename used to sit up with Copy URL, and Open File
        # / Open Folder existed only in the context menu, so the keyboard route to a finished
        # download's folder did not exist at all. Rename leads so Delete is never the first
        # item of the group.
        edit_menu.addAction(self._act_rename)
        edit_menu.addAction(self._act_move)
        edit_menu.addAction(self._act_open_file)
        edit_menu.addAction(self._act_open_folder)
        edit_menu.addAction(self._act_delete)
        edit_menu.addAction(self._act_recheck)
        edit_menu.addSeparator()

        # Queues live under Edit, not View. Every one of these operations acts on the selected
        # downloads - re-home them, change their order, create a group for them - which is what
        # this menu is for; View is for changing how the list is *drawn*. **Move Up** / **Move
        # Down** sit in this same trailing group rather than up with the transport controls,
        # because they are ordering commands, not playback ones.
        self._menu_queues = edit_menu.addMenu(_create_emoji_icon("🗃️"), "Queues")
        self._act_queue_all = QAction("All Queues", self)
        self._act_queue_all.setCheckable(True)
        self._act_queue_all.setChecked(True)
        self._act_queue_all.triggered.connect(lambda: self._on_select_queue(ALL_QUEUES))
        self._queue_scope_group = QActionGroup(self)
        self._queue_scope_group.setExclusive(True)
        self._queue_scope_group.addAction(self._act_queue_all)
        self._menu_queues.addAction(self._act_queue_all)
        self._menu_queues.addSeparator()
        # Rebuilt on every change rather than populated once, so a queue created or deleted
        # anywhere (the manager dialog, the row context menu) cannot leave a dead entry.
        self._queue_actions: list[QAction] = []

        # Held as fields because _refresh_queue_ui clears and re-adds the menu wholesale.
        self._act_new_queue = QAction(_create_emoji_icon("➕"), "New Queue…", self)
        self._act_new_queue.triggered.connect(self._on_new_queue)
        self._act_manage_queues = QAction(_create_emoji_icon("⚙️"), "Manage Queues…", self)
        self._act_manage_queues.triggered.connect(self._on_manage_queues)
        self._menu_queues.addAction(self._act_new_queue)
        self._menu_queues.addAction(self._act_manage_queues)

        # The Edit-menu twin of the row context menu's **Move to Queue**, for keyboard and
        # menu-only use. Plain actions rather than checkmarks: a multi-row selection can span
        # queues, so there is no single current queue to tick.
        self._menu_move_to_queue = edit_menu.addMenu(
            _create_emoji_icon("➡️"), "Move to Queue"
        )
        self._move_to_queue_actions: list[QAction] = []

        # Priority ordering belongs with queue membership, not with the transport controls.
        edit_menu.addAction(self._act_move_up)
        edit_menu.addAction(self._act_move_down)

        # View menu
        view_menu = menubar.addMenu("&View")
        self._menu_segregated_view = view_menu.addMenu(_create_emoji_icon("🗂️"), "Segregated View")

        self._act_segregated_view = QAction("On", self)
        self._act_segregated_view.setCheckable(True)
        self._act_segregated_view.setChecked(self._segregated_view_enabled)
        self._act_segregated_view.toggled.connect(self._on_toggle_segregated_view)
        self._menu_segregated_view.addAction(self._act_segregated_view)

        self._menu_segregated_view.addSeparator()

        self._seg_mode_group = QActionGroup(self)
        self._seg_mode_group.setExclusive(True)

        self._act_seg_by_status = QAction("Status (Active / Seeding / Inactive)", self)
        self._act_seg_by_status.setCheckable(True)
        self._act_seg_by_status.setChecked(self._segregated_view_mode == "status")
        self._act_seg_by_status.triggered.connect(lambda: self._set_segregation_mode("status"))
        self._seg_mode_group.addAction(self._act_seg_by_status)
        self._menu_segregated_view.addAction(self._act_seg_by_status)

        self._act_seg_by_date = QAction("Date (Today / Yesterday / Last 7 Days / Last 30 Days / Older)", self)
        self._act_seg_by_date.setCheckable(True)
        self._act_seg_by_date.setChecked(self._segregated_view_mode == "date")
        self._act_seg_by_date.triggered.connect(lambda: self._set_segregation_mode("date"))
        self._seg_mode_group.addAction(self._act_seg_by_date)
        self._menu_segregated_view.addAction(self._act_seg_by_date)

        self._act_seg_by_type = QAction("File Type (Video / Audio / Archives / Documents / Photos / General)", self)
        self._act_seg_by_type.setCheckable(True)
        self._act_seg_by_type.setChecked(self._segregated_view_mode == "type")
        self._act_seg_by_type.triggered.connect(lambda: self._set_segregation_mode("type"))
        self._seg_mode_group.addAction(self._act_seg_by_type)
        self._menu_segregated_view.addAction(self._act_seg_by_type)

        # One mapping of mode key -> action, so the checkmark sync in `_set_segregation_mode` and
        # the enable/disable sync here cannot drift apart as modes are added.
        self._seg_mode_actions = {
            "status": self._act_seg_by_status,
            "date": self._act_seg_by_date,
            "type": self._act_seg_by_type,
        }
        self._sync_segregation_mode_actions()

        view_menu.addAction(self._act_toggle_details)
        view_menu.addSeparator()
        select_all_act = QAction(_create_emoji_icon("☑️"), "Select All", self)
        select_all_act.setShortcut(QKeySequence("Ctrl+A"))
        select_all_act.triggered.connect(self._table.selectAll)
        view_menu.addAction(select_all_act)

        view_menu.addSeparator()
        view_menu.addAction(self._act_reset_view)

        view_menu.addSeparator()
        sort_menu = view_menu.addMenu("&Sort By")
        sort_menu.setIcon(_create_emoji_icon("↕️"))
        sort_columns = [
            ("Date Added (Default)", Col.ADDED),
            ("Name", Col.NAME),
            ("Source Domain", Col.SOURCE_DOMAIN),
            ("Size", Col.SIZE),
            ("Progress", Col.PROGRESS),
            ("Status", Col.STATUS),
            ("Speed", Col.SPEED),
            ("ETA", Col.ETA),
            ("Date Last Tried", Col.LAST_TRIED),
            ("Date Completed", Col.COMPLETED),
        ]
        for title, col_idx in sort_columns:
            act = QAction(title, self)
            act.triggered.connect(
                lambda checked=False, c=col_idx: self._sort_by_column(c)
            )
            sort_menu.addAction(act)

        sort_menu.addSeparator()
        act_asc = QAction("Ascending", self)
        act_asc.triggered.connect(
            lambda: self._set_sort_order(Qt.SortOrder.AscendingOrder)
        )
        sort_menu.addAction(act_asc)

        act_desc = QAction("Descending", self)
        act_desc.triggered.connect(
            lambda: self._set_sort_order(Qt.SortOrder.DescendingOrder)
        )
        sort_menu.addAction(act_desc)

        # Tools menu
        tools_menu = menubar.addMenu("&Tools")
        # Tools menu gets its own Preferences action rather than reusing the toolbar's:
        # the shortcut belongs to exactly one QAction, and the toolbar copy is the one that
        # carries it, so the menu keeps a separate action rather than a second registration
        # of Ctrl+,. Both spell the label out in full now that the toolbar strip has the room.
        self._act_tools_preferences = QAction(
            _create_emoji_icon("⚙️"), "Preferences…", self
        )
        self._act_tools_preferences.triggered.connect(
            lambda: self._on_open_preferences(TAB_GENERAL)
        )
        tools_menu.addAction(self._act_tools_preferences)
        # Statistics sits with Preferences rather than down with the other tools: both are
        # "look at / configure the app" entries, not ways to fetch something, and not commands
        # on the selected rows. It used to be a toolbar button reading the abbreviated
        # "Stats…"; this is the only Statistics action, spelled out because a menu label may
        # not be abbreviated. No shortcut: Ctrl+, is Preferences and this is a read-only view.
        self._act_tools_stats = QAction(_create_emoji_icon("📊"), "Statistics…", self)
        self._act_tools_stats.setToolTip(
            "Statistics: download and upload totals for today, this week, this month "
            "and this year"
        )
        self._act_tools_stats.triggered.connect(self._on_show_statistics)
        tools_menu.addAction(self._act_tools_stats)
        tools_menu.addSeparator()
        self._act_export_csv = QAction(_create_emoji_icon("📄"), "Export Selected as CSV…", self)
        self._act_export_csv.triggered.connect(self._on_export_selected_csv)
        tools_menu.addAction(self._act_export_csv)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_torrent_settings)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_youtube)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_tor)
        self._act_tor_settings = QAction(_create_emoji_icon("🧅"), "Tor Network Settings…", self)
        self._act_tor_settings.triggered.connect(self._on_open_tor_settings)
        tools_menu.addAction(self._act_tor_settings)
        tools_menu.addSeparator()
        tools_menu.addAction(self._act_network_settings)
        tools_menu.addAction(self._act_security_settings)
        tools_menu.addAction(self._act_bandwidth_settings)
        tools_menu.addAction(self._act_scheduler_settings)
        tools_menu.addSeparator()
        self._act_launch_animepahe_gui = QAction(_create_emoji_icon("🎬"), "Launch AnimePahe Downloader…", self)
        self._act_launch_animepahe_gui.triggered.connect(self._on_launch_animepahe_gui)
        tools_menu.addAction(self._act_launch_animepahe_gui)
        self._act_run_animepahe_cli = QAction(_create_emoji_icon("▶️"), "Run AnimePahe Scraper (CLI)", self)
        self._act_run_animepahe_cli.triggered.connect(self._on_start_animepahe_cli)
        tools_menu.addAction(self._act_run_animepahe_cli)
        self._act_external_tools_settings = QAction(_create_emoji_icon("🛠️"), "External Tools Settings…", self)
        self._act_external_tools_settings.triggered.connect(self._on_open_external_tools_settings)
        tools_menu.addAction(self._act_external_tools_settings)
        tools_menu.addSeparator()
        self._act_browser_settings = QAction(_create_emoji_icon("🌐"), "Browser Integration Settings…", self)
        self._act_browser_settings.triggered.connect(self._on_open_browser_settings)
        tools_menu.addAction(self._act_browser_settings)

        # Help menu
        help_menu = menubar.addMenu("&Help")
        about_act = QAction(_create_emoji_icon("ℹ️"), "About My-IDM", self)
        about_act.triggered.connect(self._on_about)
        help_menu.addAction(about_act)

        # Bandwidth Warning Corner Widget at the right edge of menubar
        self._bw_warning_btn = QPushButton("")
        self._bw_warning_btn.setFlat(True)
        self._bw_warning_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bw_warning_btn.setVisible(False)
        self._bw_warning_btn.setStyleSheet("""
            QPushButton {
                color: #ffb86c;
                font-weight: bold;
                padding: 2px 8px;
                border: 1px solid #ffb86c;
                border-radius: 3px;
                background-color: rgba(255, 184, 108, 0.15);
                margin: 2px 4px;
            }
            QPushButton:hover {
                background-color: rgba(255, 184, 108, 0.3);
            }
        """)
        self._bw_warning_btn.clicked.connect(self._on_open_bandwidth_settings)
        menubar.setCornerWidget(self._bw_warning_btn, Qt.Corner.TopRightCorner)

    def _setup_statusbar(self):
        self._status_label = QLabel("Ready")
        self._speed_label = QLabel("")
        self._speed_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._speed_label.customContextMenuRequested.connect(self._show_speed_context_menu)
        self._speed_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._speed_label.setToolTip("Total Transfer Speed (Click or right-click to set Download / Upload limits)")

        def _speed_label_mouse_press(event):
            if event.button() == Qt.MouseButton.LeftButton:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                self._show_speed_context_menu(pos)
            QLabel.mousePressEvent(self._speed_label, event)

        self._speed_label.mousePressEvent = _speed_label_mouse_press
        self._count_label = QLabel("0 Downloads, 0 Active")

        # Queue of the current selection. Its own label rather than a write to
        # _status_label, which carries transient action messages ("Copied URL", "Created
        # queue") that a selection change would immediately wipe.
        self._queue_status_label = QLabel("")
        self._queue_status_label.setToolTip(
            "Queue of the selected download. Use the toolbar combo or View → Queues to "
            "switch which queue is listed."
        )

        # Tor footer widget with button and embedded progress bar
        self._tor_footer_container = QWidget()
        tor_footer_layout = QVBoxLayout(self._tor_footer_container)
        tor_footer_layout.setContentsMargins(0, 0, 0, 0)
        tor_footer_layout.setSpacing(1)

        self._tor_status_btn = QPushButton("🧅 Tor: OFF")
        self._tor_status_btn.setFlat(True)
        self._tor_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tor_status_btn.setToolTip("Click to configure Tor Network Settings")
        self._tor_status_btn.clicked.connect(self._on_open_tor_settings)
        tor_footer_layout.addWidget(self._tor_status_btn)

        self._tor_footer_progress = QProgressBar()
        self._tor_footer_progress.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._tor_footer_progress.setFixedHeight(3)
        self._tor_footer_progress.setTextVisible(False)
        self._tor_footer_progress.setVisible(False)
        self._tor_footer_progress.setStyleSheet("""
            QProgressBar {
                background: #21252b;
                border: none;
                border-radius: 1px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #50fa7b, stop:1 #8be9fd);
                border-radius: 1px;
            }
        """)
        tor_footer_layout.addWidget(self._tor_footer_progress)

        self._vpn_status_btn = QPushButton("🌐 Net: Default")
        self._vpn_status_btn.setFlat(True)
        self._vpn_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vpn_status_btn.setToolTip(
            "Click to configure VPN & Network Settings"
        )
        self._vpn_status_btn.clicked.connect(self._on_open_network_settings)

        self._details_status_btn = QPushButton("📋 Details: ON")
        self._details_status_btn.setFlat(True)
        self._details_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._details_status_btn.setToolTip("Toggle bottom download details panel (F4)")
        self._details_status_btn.clicked.connect(self._on_toggle_details_btn_clicked)

        # AnimePahe background scraper status badge
        self._animepahe_status_btn = QPushButton("🎬 AnimePahe: Active")
        self._animepahe_status_btn.setFlat(True)
        self._animepahe_status_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._animepahe_status_btn.setToolTip(
            "AnimePahe CLI Scraper is running in the background\nClick for options (View Logs, Stop, Settings)"
        )
        self._animepahe_status_btn.clicked.connect(self._show_animepahe_status_menu)
        self._animepahe_status_btn.setVisible(False)
        self._animepahe_status_btn.setStyleSheet("""
            QPushButton {
                background: #193524;
                color: #50fa7b;
                border: 1px solid #50fa7b;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #2a4030;
                border-color: #69ff94;
            }
        """)

        status_bar = QStatusBar()
        status_bar.addWidget(self._status_label, 1)
        status_bar.addPermanentWidget(self._animepahe_status_btn)
        status_bar.addPermanentWidget(self._tor_footer_container)
        status_bar.addPermanentWidget(self._vpn_status_btn)
        status_bar.addPermanentWidget(self._details_status_btn)
        status_bar.addPermanentWidget(self._speed_label)
        status_bar.addPermanentWidget(self._queue_status_label)
        status_bar.addPermanentWidget(self._count_label)
        self.setStatusBar(status_bar)

        self._on_animepahe_status_changed(self._manager.is_animepahe_running())
        self._sync_panel_buttons()

        self._update_network_status_badge(self._manager.network_config)
        self._on_tor_config_changed(self._manager.tor_config)
        self._update_speed_label()
        self._update_count_label()

    def _setup_system_tray(self):
        """Initializes the Windows system tray icon and context menu."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon = None
            # Say so once, at startup. Without a tray icon "close to tray" silently degrades to
            # "close the app" (see `_can_hide_to_tray`), and a user who enabled the preference
            # would otherwise only discover it the first time they closed the window.
            log.warning(
                "No system tray is available on this desktop. The tray icon and close-to-tray "
                "will be unavailable; downloads will stop if My-IDM is closed."
            )
            return

        icon = get_app_icon()
        self._tray_icon = QSystemTrayIcon(icon, self)
        self._tray_icon.setToolTip("My-IDM — Download Manager")

        # Tray Context Menu
        tray_menu = QMenu(self)
        self._tray_act_toggle = QAction("🪟 Show My-IDM", self)
        self._tray_act_toggle.triggered.connect(self._toggle_show_window)
        tray_menu.addAction(self._tray_act_toggle)
        tray_menu.addSeparator()

        act_add = QAction("➕ Add Download…", self)
        act_add.setToolTip("Add a URL, magnet link, or .torrent file")
        act_add.triggered.connect(self._on_tray_add_download)
        tray_menu.addAction(act_add)
        tray_menu.addSeparator()

        self._tray_act_pause_all = QAction("⏸️ Pause All Downloads", self)
        self._tray_act_pause_all.triggered.connect(self._on_pause_all_downloads)
        tray_menu.addAction(self._tray_act_pause_all)

        self._tray_act_resume_all = QAction("▶️ Resume All Downloads", self)
        self._tray_act_resume_all.triggered.connect(self._on_resume_all_downloads)
        tray_menu.addAction(self._tray_act_resume_all)
        tray_menu.addSeparator()

        # Checkable, so the row shows the live state rather than describing an action. This is
        # the only checkable tray action in the menu; the rest mutate text in
        # `_update_tray_menu_text` because their state is not a simple on/off.
        self._tray_act_capture = QAction("🎯 Download Capture", self)
        self._tray_act_capture.setCheckable(True)
        self._tray_act_capture.setToolTip(
            "Turn download capture on or off. The browser is told within its poll interval; "
            "the same toggle is bound to your global hotkey."
        )
        self._tray_act_capture.setChecked(self._manager.browser_config.intercept_all)
        self._tray_act_capture.toggled.connect(self._on_capture_toggled)
        tray_menu.addAction(self._tray_act_capture)

        self._tray_act_clipboard = QAction("📋 Clipboard Capture", self)
        self._tray_act_clipboard.setCheckable(True)
        self._tray_act_clipboard.setToolTip(
            "Automatically add downloads when you copy one or more URLs. "
            "Only text where every line is a link is captured."
        )
        self._tray_act_clipboard.setChecked(
            self._manager.general_config.clipboard_monitor_enabled
        )
        self._tray_act_clipboard.toggled.connect(self._on_clipboard_capture_toggled)
        tray_menu.addAction(self._tray_act_clipboard)
        tray_menu.addSeparator()

        act_prefs = QAction("⚙️ Preferences…", self)
        act_prefs.triggered.connect(lambda: self._on_open_preferences(TAB_GENERAL))
        tray_menu.addAction(act_prefs)

        act_about = QAction("ℹ️ About My-IDM", self)
        act_about.triggered.connect(self._on_about)
        tray_menu.addAction(act_about)
        tray_menu.addSeparator()

        act_restart = QAction("🔄 Restart My-IDM", self)
        act_restart.triggered.connect(self._restart_app)
        tray_menu.addAction(act_restart)

        act_exit = QAction("🚪 Exit My-IDM", self)
        act_exit.triggered.connect(self._exit_app)
        tray_menu.addAction(act_exit)

        self._tray_icon.setContextMenu(tray_menu)
        # A right-click on a tray item must never activate it: the bottom two rows are
        # Restart and Exit, so a mis-landed right-click used to shut My-IDM down.
        self._tray_right_click_guard = _RightClickGuard(tray_menu)
        tray_menu.installEventFilter(self._tray_right_click_guard)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.messageClicked.connect(self._on_tray_message_clicked)

        # Thread-safe notification dispatcher
        self._sig_show_tray_notification.connect(self._do_show_tray_notification)
        from my_idm.notifications import register_notification_handler
        register_notification_handler(self.show_tray_notification)

        if self._manager.general_config.enable_system_tray:
            self._tray_icon.show()
        self._update_tray_menu_text()

    # -- capture: global hotkey + clipboard ----------------------------------

    def _setup_capture(self):
        """Create the capture subsystem and bring it in line with the stored preferences.

        Both halves are optional and both default to off, so this is a no-op unless the user
        has enabled something. The objects are held on the window (not created locally) because
        the clipboard subscription and the OS hotkey registration must outlive the call that
        set them up — a ``QObject`` with no Python reference can be collected, taking its
        timer or its registration with it.
        """
        from my_idm.clipboard_monitor import ClipboardMonitor
        from my_idm.hotkey import HotkeyRegistration

        self._clipboard_monitor = ClipboardMonitor(
            self._manager,
            max_urls=self._manager.general_config.clipboard_monitor_max_urls,
            min_file_size_kb=self._manager.general_config.clipboard_min_file_size_kb,
            ignored_extensions=self._manager.general_config.clipboard_ignored_extensions,
            parent=self,
            queue_provider=self._manager.get_active_queue,
            # Resolving a copied URL is HTTP work; the manager's loop already exists and the
            # GUI thread must not block on it.
            run_async=self._manager.run_coro_threadsafe,
        )
        self._clipboard_monitor.urls_captured.connect(self._on_clipboard_urls_captured)
        self._clipboard_monitor.urls_filtered.connect(self._on_clipboard_urls_filtered)

        self._hotkey = HotkeyRegistration(self)
        self._hotkey.triggered.connect(self._on_global_hotkey)

        if self._manager.general_config.clipboard_monitor_enabled:
            self._clipboard_monitor.start()
        if self._manager.general_config.capture_hotkey_enabled:
            self._register_hotkey()

    def _register_hotkey(self) -> tuple[bool, str]:
        """Bind the configured chord, reporting rather than swallowing a failure.

        A chord another application already owns fails here; saying nothing would leave the
        user pressing a key that does nothing.
        """
        sequence = self._manager.general_config.capture_hotkey_sequence
        ok, message = self._hotkey.register(sequence)
        if not ok:
            log.warning("Global hotkey not registered: %s", message)
            self._status_label.setText(message)
        return ok, message

    def _apply_capture_preferences(self):
        """Re-apply both capture toggles after the user edits Preferences.

        Called from the ``general_config_changed`` and ``browser_config_changed`` handlers, so
        a Preferences save takes effect without a restart.
        """
        cfg = self._manager.general_config

        if hasattr(self, "_clipboard_monitor"):
            self._clipboard_monitor.set_max_urls(cfg.clipboard_monitor_max_urls)
            self._clipboard_monitor.set_min_file_size_kb(cfg.clipboard_min_file_size_kb)
            self._clipboard_monitor.set_ignored_extensions(cfg.clipboard_ignored_extensions)

        wanted_clipboard = bool(cfg.clipboard_monitor_enabled)
        if wanted_clipboard and not self._clipboard_monitor.is_active:
            self._clipboard_monitor.start()
        elif not wanted_clipboard and self._clipboard_monitor.is_active:
            self._clipboard_monitor.stop()

        # Asked for unconditionally, so this is the same path as "enable" and a changed
        # sequence takes effect. `HotkeyRegistration.register` short-circuits when the chord is
        # unchanged, so this is cheap and does not drop and re-claim the binding every save.
        if cfg.capture_hotkey_enabled:
            self._register_hotkey()
        else:
            self._hotkey.unregister()

        self._sync_capture_actions()

    def _sync_capture_actions(self):
        """Mirror the real capture state onto the tray checkboxes.

        Called after anything that can change the state, including the hotkey, so the menu can
        never disagree with what capture is actually doing.
        """
        for action, checked in (
            (
                getattr(self, "_tray_act_capture", None),
                bool(self._manager.browser_config.intercept_all),
            ),
            (
                getattr(self, "_tray_act_clipboard", None),
                bool(self._manager.general_config.clipboard_monitor_enabled),
            ),
        ):
            if action is None or action.isChecked() == checked:
                continue
            was_blocked = action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(was_blocked)

    def _on_capture_toggled(self, checked: bool):
        """Tray checkbox, tray item or global hotkey: flip browser capture."""
        changed, message = self._manager.set_capture_enabled(checked)
        self._status_label.setText(message)
        self._sync_capture_actions()
        return changed

    def _on_clipboard_capture_toggled(self, checked: bool):
        """Tray checkbox for clipboard capture only."""
        cfg = self._manager.general_config
        cfg.clipboard_monitor_enabled = bool(checked)
        self._apply_capture_preferences()
        self._status_label.setText(
            f"Clipboard capture {'enabled' if checked else 'disabled'}."
        )

    def _on_global_hotkey(self):
        """The system-wide chord was pressed: flip capture and say so."""
        self._on_capture_toggled(not self._manager.browser_config.intercept_all)

    def _on_clipboard_urls_captured(self, urls: list, skipped: int = 0):
        """Report what clipboard capture added, so it is never silent.

        A status-bar line is easy to miss and gone in seconds; the download appeared on its own
        with nothing else to explain it. A Windows toast is the same courtesy the browser
        capture already extends, and it names **Clipboard** as the source so it is obvious where
        the row came from.
        """
        what = urls[0] if len(urls) == 1 else f"{len(urls)} URLs"
        message = f"📋 Clipboard capture added {what}"
        if skipped:
            message += f" ({skipped} over the per-copy limit were skipped)"
        self._status_label.setText(message)
        self._update_count_label()
        self._notify_clipboard_captured(urls, skipped)

    def _on_clipboard_urls_filtered(self, filtered: list):
        """Say why a copied URL was not captured.

        Silence here is what made this feature untrustworthy: the user copies a link, nothing
        appears, and there is no way to tell a deliberate skip from a broken monitor. The
        reasons come from :func:`my_idm.clipboard_monitor.decide_capture`, so they describe the
        rule that actually fired rather than a guess.
        """
        if not filtered:
            return
        _, reason = filtered[0]
        what = "URL" if len(filtered) == 1 else f"{len(filtered)} URLs"
        log.info("Clipboard capture skipped %s: %s", what, reason)
        self._status_label.setText(f"📋 Clipboard capture skipped {what}: {reason}")

    def _notify_clipboard_captured(self, urls: list, skipped: int):
        from my_idm.notifications import notify_clipboard_download_captured

        filenames = []
        for url in urls:
            entry = self._manager.find_by_url(url)
            filenames.append((entry.filename or entry.url) if entry else url)
        notify_clipboard_download_captured(filenames, skipped)

    def _release_capture(self):
        """Drop both capture halves. Called from ``closeEvent`` and app shutdown.

        A global hotkey left registered outlives the process that asked for it: Windows keeps
        the chord bound and the next launch then fails to claim it. Unregistering is therefore
        not optional, and it must happen even when the window is only being hidden.
        """
        monitor = getattr(self, "_clipboard_monitor", None)
        if monitor is not None:
            monitor.stop()
        hotkey = getattr(self, "_hotkey", None)
        if hotkey is not None:
            hotkey.unregister()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """Handles user clicking or double-clicking the system tray icon."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_show_window()

    def _on_tray_message_clicked(self):
        """Handles notification click to open and focus My-IDM window."""
        self._restore_and_focus()

    def _restore_and_focus(self):
        """Restores the window from tray/minimized state and brings it into active foreground focus."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        from my_idm.single_instance import activate_window
        activate_window(self)
        self._update_tray_menu_text()

    def _toggle_show_window(self):
        """Toggles main window visibility between foreground and hidden."""
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._restore_and_focus()
        self._update_tray_menu_text()

    def _update_tray_menu_text(self):
        """Updates the text of the tray context menu Show/Hide action dynamically."""
        if hasattr(self, "_tray_act_toggle") and self._tray_act_toggle:
            if self.isVisible() and not self.isMinimized():
                self._tray_act_toggle.setText("🪟 Hide My-IDM")
            else:
                self._tray_act_toggle.setText("🪟 Show My-IDM")

    def show_tray_notification(self, title: str, message: str, duration: int = 5, icon_path: Optional[str] = None) -> bool:
        """Handler registered with my_idm.notifications to route toasts via QSystemTrayIcon."""
        if not self._tray_icon or not self._tray_icon.isVisible():
            return False
        self._sig_show_tray_notification.emit(title, message, duration * 1000)
        return True

    def _do_show_tray_notification(self, title: str, message: str, msecs: int):
        """Displays toast on main GUI thread via QSystemTrayIcon."""
        if self._tray_icon and self._tray_icon.isVisible():
            try:
                self._tray_icon.showMessage(
                    title,
                    message,
                    QSystemTrayIcon.MessageIcon.Information,
                    msecs,
                )
            except Exception as exc:
                log.warning("Failed to show tray notification: %s", exc)

    def _restart_app(self):
        """Restarts the application."""
        import sys
        import os
        from pathlib import Path
        
        if self._tray_icon:
            self._tray_icon.hide()
        from my_idm.notifications import unregister_notification_handler
        unregister_notification_handler(self.show_tray_notification)
        
        # Use QProcess to start a detached process with --restart flag, then quit
        from PySide6.QtCore import QProcess
        
        project_root = Path(__file__).resolve().parent.parent
        entry_point = str(project_root / "run.pyw")
        python_exe = sys.executable
        args = sys.argv[1:] + ['--restart']
        
        # Start detached process using Qt's QProcess
        QProcess.startDetached(python_exe, [entry_point] + args)
        
        self._force_exit = True
        from PySide6.QtGui import QCloseEvent
        close_ev = QCloseEvent()
        self.closeEvent(close_ev)

        app = QApplication.instance()
        if app:
            app.quit()

    def _exit_app(self):
        """Forces full application exit, bypassing close-to-tray intercept."""
        self._force_exit = True
        if self._tray_icon:
            self._tray_icon.hide()
        from my_idm.notifications import unregister_notification_handler
        unregister_notification_handler(self.show_tray_notification)

        from PySide6.QtGui import QCloseEvent
        close_ev = QCloseEvent()
        self.closeEvent(close_ev)

        app = QApplication.instance()
        if app:
            app.quit()

    def _connect_signals(self):
        self._manager.progress_updated.connect(self._on_progress_updated)
        self._manager.status_changed.connect(self._on_status_changed)
        self._manager.filename_resolved.connect(self._on_filename_resolved)
        self._manager.download_added.connect(self._on_download_added)
        self._manager.torrent_folder_captured.connect(self._on_watched_folder_torrents)
        self._manager.download_removed.connect(self._on_download_removed)
        self._manager.download_moved.connect(self._on_download_moved)
        self._manager.checksum_computed.connect(self._on_checksum_computed)
        self._manager.download_renamed.connect(self._on_download_renamed)
        self._manager.download_url_updated.connect(self._on_download_url_updated)
        self._manager.network_config_changed.connect(
            self._update_network_status_badge
        )
        self._manager.tor_config_changed.connect(self._on_tor_config_changed)
        self._manager.tor_status_changed.connect(self._on_tor_status_changed)
        self._manager.threat_detected.connect(self._on_threat_detected)
        self._manager.queue_order_changed.connect(self._on_queue_order_changed)
        self._manager.queues_changed.connect(self._on_queues_changed)
        self._manager.queue_scope_changed.connect(self._on_queue_scope_changed)
        self._manager.bandwidth_limits_changed.connect(self._on_bandwidth_limits_changed)
        self._manager.bandwidth_warning.connect(self._on_bandwidth_warning)
        self._manager.bandwidth_limit_exceeded.connect(self._on_bandwidth_limit_exceeded)
        self._manager.bandwidth_warning_cleared.connect(self._on_bandwidth_warning_cleared)
        self._manager.animepahe_status_changed.connect(
            self._on_animepahe_status_changed
        )
        if hasattr(self._manager, "animepahe_queue_changed"):
            self._manager.animepahe_queue_changed.connect(
                self._on_animepahe_queue_changed
            )
        self._details_panel.mode_changed.connect(lambda _: self._sync_panel_buttons())

        # A Preferences save hands the manager a *copy* of the config, so the window has to
        # re-derive both capture toggles from the new values rather than assume anything.
        for signal in (
            getattr(self._manager, "general_config_changed", None),
            getattr(self._manager, "browser_config_changed", None),
        ):
            if signal is not None:
                signal.connect(self._apply_capture_preferences)

        # Connect table selection to bottom details panel
        self._table.selectionModel().selectionChanged.connect(
            self._on_table_selection_changed
        )

        # Periodic timer for refreshing live details panel stats (speed, peers, progress)
        self._details_timer = QTimer(self)
        self._details_timer.setInterval(1000)
        self._details_timer.timeout.connect(self._on_details_timer_tick)
        self._details_timer.start()

    # -- data loading --------------------------------------------------------

    def _load_history(self):
        entries = self._manager.get_all_entries()
        self._model.load_entries(entries)
        self._update_count_label()
        self._update_speed_label()
        self._update_action_states()

    # -- selected entries helper ---------------------------------------------

    def _selected_ids(self) -> list[str]:
        return self._model.get_selected_ids(
            self._table.selectionModel().selectedIndexes()
        )

    def _restore_selection(self, download_ids) -> None:
        """Re-select *download_ids* after a model rebuild.

        Segregated view and the header filters both rebuild the model, and a
        model reset drops the view's selection. Without this, any status change
        silently deselects the row the user was working on. Ids that are no
        longer visible (filtered out or moved to another section) are skipped.
        """
        if not download_ids:
            return
        wanted = list(dict.fromkeys(download_ids))
        sm = self._table.selectionModel()
        sm.blockSignals(True)
        try:
            sm.clearSelection()
            first_index = None
            for did in wanted:
                row = self._model.row_for_id(did)
                if row is None or row < 0:
                    continue
                index = self._model.index(row, 0)
                # Select|Rows in ONE call. A command carrying only `Rows` has no action flag
                # (Clear/Select/Deselect/Toggle) and Qt treats it as a no-op, so calling
                # select(index, Select) and then select(index, Rows) selected column 0 alone
                # and the row highlight was drawn only over the first ~45px of the row. The id
                # was still selected, so status actions hit the right downloads - only the
                # paint was wrong, which is what made it look like a cosmetic glitch.
                sm.select(
                    index,
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
                if first_index is None:
                    first_index = index
            if first_index is not None:
                # Move the current index through the selection model with NoUpdate.
                # QAbstractItemView.setCurrentIndex() defaults to ClearAndSelect|Current, so
                # the plain call wiped the selection it had just rebuilt and left one row
                # selected - invisible with a single row selected, and the reason that pausing /
                # resuming / seeding N selected downloads left exactly one of them selected, so
                # the next action applied to the wrong row. (PySide6 does not expose
                # setCurrentIndex's two-argument overload, hence going via the model.)
                sm.setCurrentIndex(
                    first_index, QItemSelectionModel.SelectionFlag.NoUpdate
                )
                self._table.scrollTo(first_index, QAbstractItemView.ScrollHint.EnsureVisible)
        finally:
            sm.blockSignals(False)
        self._on_table_selection_changed()

    def _first_selected_entry(self) -> Optional[DownloadEntry]:
        ids = self._selected_ids()
        if ids:
            return self._model.get_entry_by_id(ids[0])
        return None

    # -- sorting & column helpers --------------------------------------------

    def _on_header_filter_requested(self, column: int, selected_keys: object):
        self._update_count_label()

    def _on_section_moved(self, logical_index: int, old_visual: int, new_visual: int):
        if not hasattr(self, "_splitter"):
            return
        if old_visual != new_visual:
            self._save_ui_state_to_db()

    def _on_header_section_clicked(self, logical_index: int):
        if logical_index in Col.DATE_COLUMNS:
            # If switching to a date column from another column, ensure it defaults to Descending (newest first)
            if self._last_sort_section != logical_index:
                self._table.sortByColumn(logical_index, Qt.SortOrder.DescendingOrder)
        self._last_sort_section = logical_index

    def _sort_by_column(self, col: int):
        if col in Col.DATE_COLUMNS:
            curr_sec = self._table.horizontalHeader().sortIndicatorSection()
            curr_ord = self._table.horizontalHeader().sortIndicatorOrder()
            if curr_sec == col:
                order = (
                    Qt.SortOrder.AscendingOrder
                    if curr_ord == Qt.SortOrder.DescendingOrder
                    else Qt.SortOrder.DescendingOrder
                )
            else:
                order = Qt.SortOrder.DescendingOrder
            self._table.sortByColumn(col, order)
        else:
            current_order = self._table.horizontalHeader().sortIndicatorOrder()
            self._table.sortByColumn(col, current_order)
        self._last_sort_section = col

    def _set_sort_order(self, order: Qt.SortOrder):
        col = self._table.horizontalHeader().sortIndicatorSection()
        if col < 0:
            col = Col.ADDED
        self._table.sortByColumn(col, order)
        self._last_sort_section = col

    # -- action handlers -----------------------------------------------------

    def _on_tray_add_download(self):
        """Open the Add Download dialog from the tray menu.

        The main window is restored first: the dialog is modal and parented to it,
        so it would otherwise open behind (or be unreachable while) the hidden
        window that the tray implies.
        """
        if not self.isVisible() or self.isMinimized():
            self._restore_and_focus()
        self._on_add()

    def _on_add(self):
        dlg = AddDownloadDialog(self, manager=self._manager)
        if dlg.exec() != AddDownloadDialog.DialogCode.Accepted:
            return

        # The Add Download dialog can hand a pasted AnimePahe URL straight to
        # the AnimePahe section of Preferences. When it does, the scraper is
        # started there and the dialog closes — nothing should be added as a
        # plain HTTP download in that case.
        if getattr(dlg, "animepahe_handoff", False) is True:
            # Switch bottom panel to AnimePahe console so user sees scraper progress
            if not self._details_panel.isVisible():
                self._details_panel.setVisible(True)
                self._act_toggle_details.setChecked(True)
            self._details_panel.set_mode("console")
            self._act_toggle_details.setChecked(True)
            return

        yt_selection = getattr(dlg, "youtube_selection", None)
        if isinstance(yt_selection, dict) and yt_selection.get("videos"):
            self._queue_youtube_selection(yt_selection)
            return

        urls = getattr(dlg, "urls", [dlg.url] if dlg.url else [])
        # New downloads join whichever queue the view is scoped to. With "All Queues" selected
        # that is the default queue, so the common case is unchanged.
        queue_id = self._manager.get_active_queue()
        for u in urls:
            self._manager.add_download(
                u, dlg.save_path, dlg.num_segments, queue_id=queue_id
            )

    def _on_add_youtube(self):
        """Open the YouTube downloader directly (Tools menu / Ctrl+Y)."""
        from my_idm.youtube_dialog import YouTubeDialog

        dlg = YouTubeDialog(self, manager=self._manager)
        if dlg.exec() == YouTubeDialog.DialogCode.Accepted:
            self._queue_youtube_selection(dlg.selection())

    def _queue_youtube_selection(self, selection: dict):
        """Dispatch a confirmed YouTube dialog selection to the manager."""
        videos = selection.get("videos") or []
        if not videos:
            return
        selector = selection.get("format_selector") or ""
        save_path = selection.get("save_path") or ""
        queued = 0
        for md in videos:
            url = getattr(md, "webpage_url", "") or ""
            if not url:
                continue
            if self._manager.add_youtube_download(
                url=url,
                metadata=md,
                save_path=save_path,
                format_selector=selector,
            ):
                queued += 1
        if queued and hasattr(self, "_status_label"):
            self._status_label.setText(
                f"Queued {queued} YouTube download{'s' if queued != 1 else ''}"
            )

    def _on_add_torrent(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Torrent Files", "",
            "Torrent Files (*.torrent);;All Files (*)",
        )
        # Routed through the shared ingestion policy so a button-chosen file, a dropped one and
        # one found in the watched folder are all treated identically.
        self._add_torrent_paths(paths, source="dialog")

    # -- .torrent drag and drop -------------------------------------------------------------------
    #
    # Two regions cannot be covered by these handlers, both structural rather than oversight:
    #
    #   * The embedded browser. `DetailsPanel` reparents a native Chrome HWND into a Qt container
    #     (`EmbeddedBrowserContainer.attach_window`), and a non-Qt window generates no Qt drag
    #     events at all. No `setAcceptDrops` changes that.
    #   * Modal dialogs. Add Download and Preferences are separate top-level windows with this
    #     one as their parent, so a drop onto them is delivered to them, not here.
    #
    # Everything else in the window — the table, the details panel's Qt tabs, the toolbar, the
    # status bar, the menu bar — propagates up to MainWindow and lands in these three methods.

    def dragEnterEvent(self, event):
        """Accept a drag only when it carries at least one real local ``.torrent`` file.

        Deciding here rather than at drop time is what makes the window behave normally for
        every other drag: a payload with nothing usable is ignored, so it does not light up a
        drop cursor over the table and then do nothing when released.
        """
        if local_torrent_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """Keep the accepted drag highlighted as the pointer moves.

        Qt's default `dragMoveEvent` is a no-op, which makes an accepted drag stop looking
        accepted as soon as the cursor moves within the window.
        """
        if local_torrent_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Add the dropped ``.torrent`` files and report what happened."""
        paths = local_torrent_paths_from_mime(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._add_torrent_paths(paths, source="drop")

    def _add_torrent_paths(self, paths: list, source: str = "manual"):
        """Add local ``.torrent`` paths and tell the user.

        Silently doing nothing is never an option for an action the user performed on purpose.
        """
        added = self._manager.add_torrent_files(paths, source=source)
        if not added:
            self._status_label.setText("No .torrent files to add")
            return []
        names = [Path(p).name for p in added]
        what = names[0] if len(names) == 1 else f"{len(names)} torrent files"
        self._status_label.setText(f"🧲 Added {what}")
        self._update_count_label()
        from my_idm.notifications import notify_torrent_files_added

        notify_torrent_files_added(names, source)
        return added

    def _on_watched_folder_torrents(self, paths: list):
        """Report ``.torrent`` files the watched folder picked up.

        A watched folder is the least visible way to add a download there is — nothing was
        clicked — so without this the new row has nothing to connect it to the file that caused
        it. Names come from the table where possible: the entry is named after the torrent's own
        metadata, not after the file the user happened to save.
        """
        names = []
        for path in paths:
            entry = self._manager.find_by_url(path)
            names.append((entry.filename or Path(path).name) if entry else Path(path).name)
        what = names[0] if len(names) == 1 else f"{len(names)} torrent files"
        self._status_label.setText(f"📂 Watched folder added {what}")
        self._update_count_label()
        from my_idm.notifications import notify_torrent_files_added

        notify_torrent_files_added(names, "watched folder")

    def _on_pause(self):
        ids = self._selected_ids()
        if ids:
            self._manager.pause_downloads(ids)

    def _on_pause_all_downloads(self):
        count = self._manager.pause_all_downloads()
        if count > 0:
            self._status_label.setText(f"Paused {count} download{'s' if count > 1 else ''}")
        else:
            self._status_label.setText("No active downloads to pause")

    def _on_resume_all_downloads(self):
        count = self._manager.resume_all_downloads()
        if count > 0:
            self._status_label.setText(f"Resumed {count} download{'s' if count > 1 else ''}")
        else:
            self._status_label.setText("No paused downloads to resume")

    def _on_stop(self):
        ids = self._selected_ids()
        if ids:
            self._manager.stop_downloads(ids)

    def _on_start_seeding(self):
        for did in self._selected_ids():
            self._manager.start_seeding(did)

    def _on_stop_all_seeding(self):
        count = self._manager.stop_all_seeding()
        if count > 0:
            self._status_label.setText(f"Stopped {count} seeding torrent{'s' if count > 1 else ''}")
        else:
            self._status_label.setText("No torrents are currently seeding")

    def _on_resume(self):
        ids = self._selected_ids()
        if ids:
            self._manager.resume_downloads(ids)

    def _on_force_start(self):
        ids = self._selected_ids()
        if ids:
            self._manager.force_start_downloads(ids)

    def _on_delete(self):
        ids = self._selected_ids()
        if not ids:
            return
        dlg = DeleteConfirmDialog(len(ids), self)
        if dlg.exec() == DeleteConfirmDialog.DialogCode.Accepted:
            self._manager.delete_downloads(ids, dlg.delete_files)

    def _on_delete_file(self):
        ids = self._selected_ids()
        if not ids:
            return
        items_str = "this file" if len(ids) == 1 else f"the files for these {len(ids)} downloads"
        ans = QMessageBox.question(
            self,
            "Delete File",
            f"Are you sure you want to delete {items_str} from disk (move to Trash)?\n\n"
            "The download entry will be kept, paused, and progress reset to 0.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._manager.delete_download_files(ids)

    def _on_move(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        dlg = MoveDownloadDialog(entry.save_path, self, self._manager._db)
        if dlg.exec() == MoveDownloadDialog.DialogCode.Accepted:
            for did in self._selected_ids():
                self._manager.move_download(did, dlg.new_path)

    def _on_recheck(self):
        ids = self._selected_ids()
        if ids:
            self._manager.recheck_downloads(ids)

    def _on_move_queue_up(self):
        for did in self._selected_ids():
            self._manager.move_queue_up(did)

    def _on_move_queue_down(self):
        for did in reversed(self._selected_ids()):
            self._manager.move_queue_down(did)

    def _on_queue_order_changed(self):
        entries = self._manager.get_all_entries()
        self._model.load_entries(entries)
        self._update_count_label()

    def _on_open_file(self):
        entry = self._first_selected_entry()
        if entry:
            if entry.file_path and Path(entry.file_path).exists():
                # Was `os.startfile`, which is Windows-only — an AttributeError elsewhere.
                # create_if_missing=False: the file is known to exist, and the default would
                # create an empty placeholder if it disappeared before the call.
                from my_idm.external_tools import open_file_in_default_app

                open_file_in_default_app(entry.file_path, create_if_missing=False)
            else:
                self._manager.mark_file_not_found(entry.id)

    def _apply_table_spans(self):
        self._table.clearSpans()
        if not self._model.is_segregated_view():
            return
        for row in self._model.get_section_header_row_indices():
            self._table.setSpan(row, 0, 1, Col.COUNT)

    def _on_select_all_in_section(self, row: int) -> list[str]:
        """Select all downloads in the section header at *row*."""
        hdr = self._model.get_section_header(row)
        if hdr is None or getattr(hdr, "section_count", 0) <= 0:
            return []
        sec_id = hdr.section_id

        # If collapsed, expand so its rows become visible in the table and selectable
        if hdr.section_collapsed:
            self._model.set_section_collapsed(sec_id, False)
            self._manager.db.set_ui_state(f"segregated_{sec_id}_collapsed", False)
            self._apply_table_spans()

        download_rows = self._model.get_section_download_rows(sec_id)
        if not download_rows:
            return []

        sm = self._table.selectionModel()
        ctrl_held = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
        if not ctrl_held:
            sm.clearSelection()

        from PySide6.QtCore import QItemSelection
        selection = QItemSelection()
        for r in download_rows:
            selection.select(self._model.index(r, 0), self._model.index(r, Col.COUNT - 1))
        sm.select(
            selection,
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        self._on_table_selection_changed()
        return [self._model.entries[r].id for r in download_rows if 0 <= r < len(self._model.entries)]

    def _on_table_clicked(self, index, click_pos=None):
        row = index.row()
        if self._model.is_section_header_row(row):
            pos = click_pos if click_pos is not None else self._table.viewport().mapFromGlobal(QCursor.pos())
            cell_rect = self._table.visualRect(index)
            btn_rect = get_section_select_all_btn_rect(cell_rect, self._table.viewport().width())
            hdr = self._model.get_section_header(row)
            if hdr and getattr(hdr, "section_count", 0) > 0 and btn_rect.contains(pos):
                self._on_select_all_in_section(row)
                return
            res = self._model.toggle_section_collapsed(row)
            if res:
                sec_id, is_col = res
                self._manager.db.set_ui_state(f"segregated_{sec_id}_collapsed", is_col)
            self._apply_table_spans()

    def _on_table_double_clicked(self, index, click_pos=None):
        if self._model.is_section_header_row(index.row()):
            pos = click_pos if click_pos is not None else self._table.viewport().mapFromGlobal(QCursor.pos())
            cell_rect = self._table.visualRect(index)
            btn_rect = get_section_select_all_btn_rect(cell_rect, self._table.viewport().width())
            hdr = self._model.get_section_header(index.row())
            if hdr and getattr(hdr, "section_count", 0) > 0 and btn_rect.contains(pos):
                return
            res = self._model.toggle_section_collapsed(index.row())
            if res:
                sec_id, is_col = res
                self._manager.db.set_ui_state(f"segregated_{sec_id}_collapsed", is_col)
            self._apply_table_spans()
            return
        self._on_open_file()

    def _segregation_mode_label(self, mode: str) -> str:
        """Short human name of a segregation mode, for the status bar.

        Shares one dict with ``_set_segregation_mode``'s label: the View-menu handler and the
        Preferences tab both drive the same state, and they previously used two different
        lookups, so the "type" mode was announced as "Date".
        """
        return {"status": "Status", "date": "Date", "type": "File Type"}.get(
            mode, "Status"
        )

    def _sync_segregation_mode_actions(self, enabled: Optional[bool] = None):
        """Enable the three mode actions only while segregated view is on.

        The modes are meaningless while segregation is off - picking one has nothing to group by -
        so offering them invites a change that appears to do nothing. Their *checkmark* is
        deliberately left alone: it records the mode that will be restored when segregation is
        turned back on, and clearing it would silently lose the user's choice.

        Mirrors the Preferences Views tab, where the same coupling already exists between
        `_seg_enabled_cb` and `_seg_mode_combo`.
        """
        if enabled is None:
            enabled = self._segregated_view_enabled
        for action in self._seg_mode_actions.values():
            action.setEnabled(enabled)

    def _set_segregation_mode(self, mode: str):
        if mode not in SEGREGATED_MODES:
            mode = DEFAULT_SEGREGATED_MODE
        self._segregated_view_mode = mode
        self._manager.db.set_ui_state("segregated_view_mode", mode)
        for value, action in self._seg_mode_actions.items():
            action.setChecked(mode == value)

        if not self._segregated_view_enabled:
            # Turning a mode on implies turning segregation on. Unreachable from the View menu,
            # which greys these actions out while segregation is off, but kept for programmatic
            # callers - and it must never run while a caller is deliberately applying the user's
            # *unchecked* box, which is why SettingsDialog._apply_views_tab calls
            # _on_toggle_segregated_view first.
            self._act_segregated_view.setChecked(True)
        else:
            self._model.set_segregated_mode(mode)
            self._apply_table_spans()
            self._status_label.setText(
                f"Segregated view grouped by {self._segregation_mode_label(mode)}"
            )

    def _on_toggle_segregated_view(self, checked: bool):
        self._segregated_view_enabled = checked
        self._manager.db.set_ui_state("segregated_view_enabled", checked)
        # Keep the View-menu checkmark in step. This handler is driven by the action's
        # own `toggled` signal *and* called programmatically (state restore, and the
        # Preferences checkbox), so without this the menu can disagree with the table - and
        # a disagreement is worse than useless: `_set_segregation_mode` turns segregation
        # on by checking the action, which emits nothing when it is already checked, so
        # the enable silently did not happen. blockSignals keeps this from re-entering.
        action = getattr(self, "_act_segregated_view", None)
        if action is not None and action.isChecked() != checked:
            action.blockSignals(True)
            try:
                action.setChecked(checked)
            finally:
                action.blockSignals(False)
        selected = self._selected_ids()
        self._model.set_segregated_view(checked, mode=self._segregated_view_mode)
        self._restore_selection(selected)
        self._apply_table_spans()
        # The mode actions follow the enable flag. Done here rather than in the action's own
        # handler so the Preferences checkbox and the View menu cannot disagree - both drive
        # this method, and only this method knows the resulting state.
        self._sync_segregation_mode_actions(checked)
        if checked:
            mode_str = self._segregation_mode_label(self._segregated_view_mode)
            self._status_label.setText(f"Segregated view enabled ({mode_str})")
        else:
            self._status_label.setText("Segregated view disabled")

    def _on_export_selected_csv(self):
        selected_ids = self._selected_ids()
        selected = [self._manager.get_entry(did) for did in selected_ids]
        selected = [e for e in selected if e]
        if not selected:
            QMessageBox.information(
                self,
                "Export Selected to CSV",
                "Please select one or more downloads to export.",
            )
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Selected Downloads as CSV",
            "downloads_export.csv",
            "CSV Files (*.csv);;All Files (*)",
        )
        if not file_path:
            return

        import csv
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Name", "URL/Magnet"])
                for entry in selected:
                    name = DownloadTableModel.get_original_name(entry)
                    writer.writerow([name, entry.url or ""])
            self._status_label.setText(
                f"Exported {len(selected)} download(s) to '{Path(file_path).name}'"
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Export Failed",
                f"Could not export downloads to CSV:\n\n{exc}",
            )

    def _on_open_folder(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        from my_idm.external_tools import show_in_folder

        folder = entry.save_path
        file_path = entry.file_path
        if file_path and Path(file_path).exists():
            # Pass the file, not the folder, so the platform can select it. The old Windows branch
            # sent `explorer /select,` as its own argv element (Explorer tolerates that by
            # accident) and the non-Windows branch called `os.startfile`, which does not exist
            # outside Windows.
            show_in_folder(file_path)
        elif folder and Path(folder).exists():
            show_in_folder(folder)

    def _on_load_backlog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Backlog File", "",
            "Text Files (*.txt);;All Files (*)",
        )
        if path:
            count = self._manager.load_backlog(path)
            self._status_label.setText(
                f"Loaded {count} download(s) from backlog"
            )
            if count > 0:
                from my_idm.notifications import notify_backlog_downloads_picked
                notify_backlog_downloads_picked(count)

    def _on_about(self):
        # Reachable from the tray, where the window may be hidden; the dialog is
        # parented to it and would otherwise open unreachable.
        if not self.isVisible() or self.isMinimized():
            self._restore_and_focus()
        dlg = QMessageBox(self)
        dlg.setWindowTitle("About My-IDM")
        logo_pm = get_app_logo_pixmap(72)
        if not logo_pm.isNull():
            dlg.setIconPixmap(logo_pm)
        dlg.setText("<h3>My-IDM — Download Manager</h3>")
        dlg.setInformativeText(
            "<p><b>Version 1.0.0</b></p>"
            "<p>A high-speed download manager featuring multi-segment parallel HTTP "
            "downloading, full BitTorrent swarm engine, Tor & VPN Kill Switch privacy protection, Youtube & AnimePahe scrapers "
            "and automated virus & malware inspection.</p>"
            "<p>Built with Python, PySide6, asyncio/aiohttp, and libtorrent.</p>"
            "<p style='color: #8fa0b5; margin-top: 8px;'>© Rakesh Malik, 2026</p>"
            "<p style='color: #8fa0b5; margin-top: 14px; margin-bottom: 0px;'>"
            "<b>Co-authored with</b></p>"
            "<p style='color: #8fa0b5; margin-top: 3px; margin-bottom: 0px; margin-left: 8px;'>"
            "•&nbsp; Gemini 3.8 Flash, Claude 4.6 Opus "
            "<span style='color: #6e7681;'>(via Antigravity IDE)</span></p>"
            "<p style='color: #8fa0b5; margin-top: 3px; margin-bottom: 0px; margin-left: 8px;'>"
            "•&nbsp; Nvidia Nemotron 3 Ultra, Space Bunny Alpha, Poolside Laguna S 2.1 "
            "<span style='color: #6e7681;'>(via Kilo Code plugin for Antigravity IDE)</span></p>"
        )
        dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
        dlg.exec()

    def _on_copy_url(self):
        ids = self._selected_ids()
        if not ids:
            return
        urls: list[str] = []
        for did in ids:
            entry = self._manager.get_entry(did)
            if entry and entry.url:
                urls.append(entry.url)
        if urls:
            text = "\n".join(urls)
            # Tell the clipboard monitor this text is ours. Deduping in add_download is not
            # enough: re-adding a *paused* download resumes it, so Ctrl+C on a paused row would
            # silently start it.
            monitor = getattr(self, "_clipboard_monitor", None)
            if monitor is not None:
                monitor.suppress(text)
            clipboard = QGuiApplication.clipboard()
            if clipboard:
                clipboard.setText(text)
                if len(urls) == 1:
                    kind = "Magnet link" if urls[0].startswith("magnet:") else "URL"
                    self._status_label.setText(f"Copied {kind} to clipboard")
                else:
                    self._status_label.setText(f"Copied {len(urls)} URLs/Magnets to clipboard")

    def _on_refresh_address(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        if entry.download_type != "http":
            QMessageBox.information(
                self,
                "Refresh Address",
                "Address refresh is only applicable to HTTP/HTTPS downloads.",
            )
            return
        if entry.status in ("downloading", "scanning", "checking"):
            QMessageBox.information(
                self,
                "Refresh Address",
                "Please pause the download before refreshing its address.",
            )
            return
        dlg = RefreshAddressDialog(
            current_url=entry.url,
            filename=entry.filename or entry.id,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_url = dlg.new_url.strip()
            if new_url and new_url != entry.url:
                if self._manager.update_download_url(entry.id, new_url, resume=dlg.resume_immediately):
                    self._status_label.setText(f"Updated address for '{entry.filename or entry.id}'")
            elif new_url == entry.url and dlg.resume_immediately and entry.status in ("paused", "error", "stopped"):
                self._manager.resume_download(entry.id)

    def _on_rename(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        current_name = entry.filename or (os.path.basename(entry.file_path) if entry.file_path else "")
        dlg = RenameDialog(current_name, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_name = dlg.new_name.strip()
            if new_name and new_name != current_name:
                success, err = self._manager.rename_download(entry.id, new_name)
                if not success:
                    QMessageBox.warning(
                        self,
                        "Rename Failed",
                        f"Could not rename '{current_name}':\n\n{err}",
                    )
                else:
                    self._status_label.setText(f"Renamed to '{new_name}'")

    # -- context menu --------------------------------------------------------

    def _show_context_menu(self, pos):
        idx = self._table.indexAt(pos)
        if idx.isValid():
            if self._model.is_section_header_row(idx.row()):
                sec_menu = QMenu(self)
                entry = self._model._entries[idx.row()]
                action_word = "Expand" if entry.section_collapsed else "Collapse"
                act_this = QAction(f"{action_word} '{entry.section_title}' Section", self)
                def _toggle_this():
                    res = self._model.toggle_section_collapsed(idx.row())
                    if res:
                        sec_id, is_col = res
                        self._manager.db.set_ui_state(f"segregated_{sec_id}_collapsed", is_col)
                    self._apply_table_spans()
                act_this.triggered.connect(_toggle_this)
                sec_menu.addAction(act_this)
                if getattr(entry, "section_count", 0) > 0:
                    act_select_section = QAction(f"Select All in '{entry.section_title}'", self)
                    act_select_section.triggered.connect(lambda _, r=idx.row(): self._on_select_all_in_section(r))
                    sec_menu.addAction(act_select_section)
                sec_menu.addSeparator()
                current_mode = self._model.segregated_mode()
                if current_mode == "status":
                    active_sec_ids = ("active", "seeding", "inactive")
                elif current_mode == "type":
                    active_sec_ids = tuple(s[0] for s in TYPE_SECTION_DEFS)
                else:
                    active_sec_ids = tuple(s[0] for s in DATE_SECTION_DEFS)

                act_expand_all = QAction("Expand All Sections", self)
                def _expand_all():
                    for sid in active_sec_ids:
                        self._model.set_section_collapsed(sid, False)
                        self._manager.db.set_ui_state(f"segregated_{sid}_collapsed", False)
                    self._apply_table_spans()
                act_expand_all.triggered.connect(_expand_all)
                sec_menu.addAction(act_expand_all)

                act_collapse_all = QAction("Collapse All Sections", self)
                def _collapse_all():
                    for sid in active_sec_ids:
                        self._model.set_section_collapsed(sid, True)
                        self._manager.db.set_ui_state(f"segregated_{sid}_collapsed", True)
                    self._apply_table_spans()
                act_collapse_all.triggered.connect(_collapse_all)
                sec_menu.addAction(act_collapse_all)

                sec_menu.addSeparator()
                mode_menu = sec_menu.addMenu("Switch Grouping Mode")
                for m_key in SEGREGATED_MODES:
                    m_label = SEGREGATED_MODE_LABELS.get(m_key, m_key.capitalize())
                    act_m = mode_menu.addAction(m_label)
                    act_m.setCheckable(True)
                    act_m.setChecked(current_mode == m_key)
                    act_m.triggered.connect(lambda checked=False, m=m_key: self._set_segregation_mode(m))

                sec_menu.exec(self._table.viewport().mapToGlobal(pos))
                return

            sm = self._table.selectionModel()
            selected_rows = {i.row() for i in sm.selectedRows()}
            if idx.row() not in selected_rows:
                self._table.selectRow(idx.row())
        else:
            return

        self._update_action_states()
        menu = QMenu(self)
        menu.addAction(self._act_resume)
        menu.addAction(self._act_force_start)
        menu.addAction(self._act_pause)
        menu.addAction(self._act_stop)
        menu.addAction(self._act_start_seeding)
        menu.addSeparator()
        menu.addAction(self._act_copy_url)
        menu.addAction(self._act_refresh_address)
        menu.addAction(self._act_export_csv)
        menu.addSeparator()
        menu.addAction(self._act_scan_antivirus)
        menu.addAction(self._act_recheck)
        menu.addSeparator()
        menu.addAction(self._act_toggle_details)
        menu.addSeparator()
        # Bandwidth Allocation Submenu
        bw_menu = menu.addMenu("Bandwidth Allocation")
        entry = self._first_selected_entry()
        curr_alloc = (entry.metadata.get("bandwidth_allocation", "max") if entry and entry.metadata else "max").lower()
        alloc_options = [
            ("Low (25%)", "low"),
            ("Medium (50%)", "medium"),
            ("High (75%)", "high"),
            ("Max (100%)", "max"),
        ]
        active_alloc_statuses = {
            "downloading", "fetching_metadata", "stalled", "checking", "scanning", "queued", "paused", "seeding"
        }
        can_alloc = bool(entry and entry.status in active_alloc_statuses)
        bw_menu.setEnabled(can_alloc)
        for label, val in alloc_options:
            act = bw_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(curr_alloc == val)
            act.triggered.connect(lambda checked=False, a=val: self._on_set_bandwidth_allocation(a))
        menu.addSeparator()

        # Move to Queue. Lists real queues rather than asking for a name, so a typo cannot
        # create a queue the user did not mean. Built fresh per invocation for the same reason
        # the Tor submenu above is.
        queue_menu = menu.addMenu("Move to Queue")
        current_queue = (entry.queue_id or DEFAULT_QUEUE_ID) if entry else DEFAULT_QUEUE_ID
        for queue in self._manager.get_queues():
            icon = create_color_swatch_icon(
                queue.color,
                size=18,
                radius=4,
                letter=(queue.name[:1].upper() if queue.name else ""),
            )
            q_act = queue_menu.addAction(icon, queue.name)
            q_act.setCheckable(True)
            q_act.setChecked(queue.id == current_queue)
            q_act.triggered.connect(
                lambda checked=False, qid=queue.id: self._on_move_selected_to_queue(qid)
            )
        queue_menu.addSeparator()
        q_new = queue_menu.addAction("New Queue…")
        q_new.triggered.connect(self._on_new_queue)
        # Priority ordering joins queue membership here, in the same order the Edit menu uses
        # for its trailing queue group. It used to sit up with Pause / Stop / Start Seeding,
        # where two ordering commands read as transport controls and split the queue commands
        # across the menu into two groups.
        menu.addAction(self._act_move_up)
        menu.addAction(self._act_move_down)
        menu.addSeparator()

        # Per-download Tor routing. Built fresh on each invocation because the
        # enabled/checked state is per-row and Tor can start or stop at any time.
        menu.addAction(self._build_download_tor_action(entry))
        menu.addSeparator()
        # One file-operations group, in the order the Edit menu uses: rename it, move it,
        # look at it, destroy it. Rename and Move were up with Copy URL and with
        # Scan/Recheck, which split the file commands across three groups. No separators
        # inside it, and it stays last so Delete is still the final item in the menu.
        # `Delete File` is context-menu-only: the Edit menu's Delete already takes the row,
        # and a second delete that means something else is one more destructive item to keep
        # straight.
        menu.addAction(self._act_rename)
        menu.addAction(self._act_move)
        menu.addAction(self._act_open_file)
        menu.addAction(self._act_open_folder)
        menu.addAction(self._act_delete_file)
        menu.addAction(self._act_delete)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _build_download_tor_action(self, entry: Optional[DownloadEntry]) -> QAction:
        """Build the per-download 'Route through Tor' menu item.

        A fresh action is created per menu invocation because the checked state
        is per-row and the enabled state depends on whether a Tor proxy is
        reachable at this moment. The item is only enabled while Tor is running;
        when it is not, the tooltip explains why rather than silently greying out.
        """
        available = self._manager.tor_available()
        flagged = self._manager.is_download_tor_routed(entry.id) if entry else False

        # An emoji glyph in the *label* would add width on top of the icon
        # column and push this row out of alignment with the rest of the menu,
        # so the onion goes in the icon slot like every other item here.
        act = QAction(_create_emoji_icon("🧅"), "Route through Tor", self)
        act.setCheckable(True)
        act.setChecked(flagged)
        act.setEnabled(available)
        if available:
            act.setToolTip(
                "Route this download through the Tor SOCKS5 proxy.\n"
                f"{'Applies to every download.' if self._manager.tor_config.enabled else 'Affects this download only.'}"
            )
        else:
            act.setToolTip(
                "Unavailable: Tor is not running.\n"
                "Start Tor from Tools → Tor, or set its path in Tools → Tor Network Settings."
            )
        act.triggered.connect(
            lambda checked=False, did=(entry.id if entry else ""): self._on_toggle_download_tor(did, checked)
        )
        return act

    def _on_toggle_download_tor(self, download_id: str, enabled: bool):
        """Route or unroute a single download through Tor."""
        if not download_id:
            return
        ok, message = self._manager.set_download_tor_route(download_id, enabled)
        if not ok:
            QMessageBox.warning(self, "Tor Routing Unavailable", message)
        elif message:
            self._status_label.setText(message)
        self._refresh_tor_routed_rows()

    def _refresh_tor_routed_rows(self) -> None:
        """Repaint rows so the Tor badge reflects a route change immediately."""
        self._table.viewport().update()

    def _on_set_bandwidth_allocation(self, allocation: str):
        for did in self._selected_ids():
            self._manager.set_download_bandwidth_allocation(did, allocation)
        self._table.viewport().update()

    # -- signal handlers from manager ----------------------------------------

    def _on_progress_updated(self, download_id: str, downloaded: int,
                             total: int, speed: float, eta: float,
                             seeds: int, peers: int, upload_speed: float):
        self._model.update_progress(
            download_id, downloaded, total, speed, eta,
            seeds, peers, upload_speed,
        )
        self._update_speed_label()
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

    def _on_status_changed(self, download_id: str, status: str,
                              error_msg: str):
        # The manager's row is already updated by the time this slot runs, so the model's
        # copy is the only place the *previous* status still exists.
        previous_entry = self._model.get_entry_by_id(download_id)
        previous_status = previous_entry.status if previous_entry is not None else None
        if previous_status == status and not error_msg:
            return

        selected = self._selected_ids()
        model_reset = bool(self._model.update_status(download_id, status, error_msg))
        fresh = self._manager.get_entry(download_id)
        if fresh is not None:
            fresh.status = status
            if error_msg:
                fresh.error_message = error_msg
            model_reset = bool(self._model.refresh_entry(download_id, fresh)) or model_reset

        if model_reset:
            self._restore_selection(selected)
        self._update_count_label()
        self._update_speed_label()
        self._update_action_states()
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

        # "seeding" is a completion: the payload arrived. "completed" arriving *from*
        # "seeding" is not - the download finished when it started seeding and the user was
        # told then. The old code only inspected the new status, so a seeding torrent that
        # went straight to "seeding" on finishing never notified at all, and then notified a
        # second time when its seeding session ended (time limit, ratio limit, or the user
        # stopping it). Both halves were wrong, in opposite directions.
        finished_now = status in ("completed", "seeding") and previous_status not in (
            "completed", "seeding",
        )
        if finished_now:
            if (
                self._manager.general_config.notify_on_completion
                and download_id not in self._completed_notified
            ):
                entry = self._manager.get_entry(download_id)
                fname = entry.filename if entry and entry.filename else download_id
                from my_idm.notifications import notify_download_complete
                notify_download_complete(fname)
            # Record it even when notifications are off or suppressed, so a later
            # completed/seeding bounce cannot resurrect one.
            self._completed_notified.add(download_id)
        elif status in ("downloading", "queued", "paused", "stopped"):
            # Work (re)started, so the next completion is news again.
            self._completed_notified.discard(download_id)

    def _on_filename_resolved(self, download_id: str, filename: str):
        self._model.update_filename(download_id, filename)
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

    def _on_checksum_computed(self, download_id: str, checksum: str):
        fresh = self._manager.get_entry(download_id)
        if fresh is not None:
            self._model.refresh_entry(download_id, fresh)
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()

    def _on_download_added(self, download_id: str):
        entry = self._manager.get_entry(download_id)
        if entry:
            self._model.add_entry(entry)
            self._update_count_label()
            self._update_action_states()

    # -- queues ---------------------------------------------------------------

    def _init_queue_scope(self):
        """Restore the persisted queue scope and populate the switcher, once, at startup.

        Without this the combo is empty until some event repopulates it, and the model keeps
        whatever scope it happened to be constructed with.
        """
        active = self._manager.get_active_queue()
        self._model.set_queue_scope(active)
        self._refresh_queue_ui()

    def _refresh_queue_ui(self):
        """Rebuild the Edit ▸ Queues menu from the manager's state.

        Rebuilt wholesale rather than diffed: the list is small, and a diff is where a queue
        renamed elsewhere would keep showing its old name until a restart.

        The menu is rebuilt by clearing it and re-adding the same ``QAction`` objects in order.
        The earlier version removed and re-inserted actions individually, and `list.clear()`
        inside the removal loop ended the iteration after one action - so every refresh left a
        stale duplicate behind and shuffled "All Queues" down the menu.

        This used to also refill a queue-switcher combo in the toolbar. There is no combo now:
        the menu is the only scope switcher, so the exclusive ``QActionGroup`` below is the
        single source of truth for which queue is selected, and the toolbar stays a row of
        transport and file commands.

        Also refreshes the model's queue_id -> name map: a rename changes what every row of that
        queue shows in ``Col.QUEUE_NAME`` at once, so the two must not drift.
        """
        queues = self._manager.get_queues()
        active = self._manager.get_active_queue()
        self._model.set_queue_names({q.id: q.name for q in queues})
        self._model.set_queue_colors({q.id: q.color for q in queues})

        # Retire the previous per-queue actions from the exclusive group before dropping them,
        # so the group does not accumulate dead actions holding their lambdas alive.
        for action in self._queue_actions:
            self._queue_scope_group.removeAction(action)
        self._queue_actions = []
        self._move_to_queue_actions = []

        self._act_queue_all.setChecked(active == ALL_QUEUES)
        self._menu_queues.clear()
        self._menu_queues.addAction(self._act_queue_all)
        self._menu_queues.addSeparator()
        for queue in queues:
            icon = create_color_swatch_icon(
                queue.color,
                size=18,
                radius=4,
                letter=(queue.name[:1].upper() if queue.name else ""),
            )
            action = QAction(icon, queue.name, self)
            action.setCheckable(True)
            action.setChecked(queue.id == active)
            action.triggered.connect(
                lambda _checked=False, qid=queue.id: self._on_select_queue(qid)
            )
            self._queue_scope_group.addAction(action)
            self._menu_queues.addAction(action)
            self._queue_actions.append(action)
        self._menu_queues.addSeparator()
        self._menu_queues.addAction(self._act_new_queue)
        self._menu_queues.addAction(self._act_manage_queues)

        self._menu_move_to_queue.clear()
        for queue in queues:
            icon = create_color_swatch_icon(
                queue.color,
                size=18,
                radius=4,
                letter=(queue.name[:1].upper() if queue.name else ""),
            )
            action = QAction(icon, queue.name, self)
            action.triggered.connect(
                lambda _checked=False, qid=queue.id: self._on_move_selected_to_queue(qid)
            )
            self._menu_move_to_queue.addAction(action)
            self._move_to_queue_actions.append(action)
        # Nothing selected means nothing to move. The actions grey out rather than the whole
        # submenu, so the menu does not change shape under the user's cursor.
        self._update_move_to_queue_enabled()

    def _update_action_states(self):
        """Update enabled states of toolbar, menu, and context menu actions based on selection and status."""
        if not hasattr(self, "_act_resume"):
            return

        selected_ids = self._selected_ids()
        selected_entries = [
            e for did in selected_ids
            if (e := self._model.get_entry_by_id(did)) is not None
        ]
        all_entries = self._model.all_entries

        has_selection = len(selected_entries) > 0
        single_selection = len(selected_entries) == 1

        resumable_statuses = {
            "paused", "stopped", "error", "file_not_found", "threat_detected", "suspended"
        }
        pausable_statuses = {
            "downloading", "fetching_metadata", "stalled", "checking", "scanning", "queued", "seeding"
        }
        active_non_seeding_statuses = {
            "downloading", "fetching_metadata", "stalled", "checking", "scanning", "queued"
        }
        stoppable_statuses = {
            "downloading", "fetching_metadata", "stalled", "checking", "scanning", "queued", "paused", "seeding"
        }
        force_startable_statuses = {
            "paused", "stopped", "queued", "error", "file_not_found", "threat_detected", "stalled", "suspended"
        }

        can_resume = has_selection and any(e.status in resumable_statuses for e in selected_entries)
        can_pause = has_selection and any(e.status in pausable_statuses for e in selected_entries)
        can_stop = has_selection and any(e.status in stoppable_statuses for e in selected_entries)
        can_force_start = has_selection and any(e.status in force_startable_statuses for e in selected_entries)
        can_seed = has_selection and any(
            e.download_type == "torrent" and e.status in ("completed", "paused", "stopped")
            for e in selected_entries
        )

        has_existing_file = any(e.status != "file_not_found" for e in selected_entries)
        single_has_existing_file = single_selection and selected_entries[0].status != "file_not_found"

        self._act_resume.setEnabled(can_resume)
        self._act_pause.setEnabled(can_pause)
        self._act_stop.setEnabled(can_stop)
        self._act_force_start.setEnabled(can_force_start)
        self._act_start_seeding.setEnabled(can_seed)

        self._act_copy_url.setEnabled(has_selection)
        can_refresh_address = (
            single_selection
            and selected_entries[0].download_type == "http"
            and selected_entries[0].status not in ("downloading", "scanning", "checking")
        )
        self._act_refresh_address.setEnabled(can_refresh_address)
        self._act_rename.setEnabled(single_has_existing_file)
        self._act_delete.setEnabled(has_selection)
        self._act_delete_file.setEnabled(has_selection and has_existing_file)
        self._act_move.setEnabled(has_selection and has_existing_file)
        # Recheck deliberately does NOT gate on has_existing_file. Gating it there was exactly
        # backwards: recheck is the one action that inspects the disk, so it is the way out of
        # "file not found" - the user restores or moves the file back and rechecks, and
        # DownloadManager.recheck_download flips the row to completed (or resets it to queued
        # with the reason, if the file really is still gone). With the gate on, the only way
        # back was to re-download the whole thing.
        self._act_recheck.setEnabled(has_selection)
        self._act_open_file.setEnabled(single_has_existing_file)
        self._act_open_folder.setEnabled(has_selection and has_existing_file)
        self._act_scan_antivirus.setEnabled(has_selection and has_existing_file)
        self._act_move_up.setEnabled(has_selection)
        self._act_move_down.setEnabled(has_selection)
        self._update_move_to_queue_enabled()

        has_resumable = any(e.status in resumable_statuses for e in all_entries)
        has_pausable_non_seeding = any(e.status in active_non_seeding_statuses for e in all_entries)
        has_seeding = any(e.status == "seeding" for e in all_entries)

        self._act_resume_all.setEnabled(has_resumable)
        self._act_pause_all.setEnabled(has_pausable_non_seeding)
        self._act_stop_all_seeding.setEnabled(has_seeding)
        if hasattr(self, "_tray_act_resume_all"):
            self._tray_act_resume_all.setEnabled(has_resumable)
        if hasattr(self, "_tray_act_pause_all"):
            self._tray_act_pause_all.setEnabled(has_pausable_non_seeding)

    def _update_move_to_queue_enabled(self):
        """Grey out **Move to Queue**'s actions when there is nothing selected.

        The actions rather than the submenu: disabling the submenu itself is what made it look
        "not clickable", because a disabled submenu gives no hint that selecting a row would
        enable it. Called on every selection change, not only when the menu is rebuilt — nothing
        else re-ran it between clicking a row and reaching for the menu.
        """
        enabled = bool(self._selected_ids())
        for action in getattr(self, "_move_to_queue_actions", []):
            action.setEnabled(enabled)
        menu = getattr(self, "_menu_move_to_queue", None)
        if menu is not None:
            menu.setEnabled(enabled)

    def _on_select_queue(self, queue_id: str):
        """Scope the downloads list to *queue_id* (``""`` for all queues)."""
        self._manager.set_active_queue(queue_id)
        self._refresh_queue_ui()

    def _on_queue_scope_changed(self, queue_id: str):
        """The scope changed from anywhere (menu, combo, a deleted queue)."""
        self._model.set_queue_scope(queue_id)
        self._load_history()
        self._refresh_queue_ui()
        self._update_count_label()

    def _on_queues_changed(self):
        self._refresh_queue_ui()
        self._load_history()
        self._update_count_label()

    def _on_new_queue(self):
        dlg = AddQueueDialog(self, manager=self._manager)
        if not dlg.exec():
            return
        created, message = self._manager.create_queue(
            dlg.name.strip(), dlg.max_concurrent, dlg.color,
            dlg.download_limit_kb * 1024, dlg.upload_limit_kb * 1024,
        )
        self._status_label.setText(message)
        if created:
            self._refresh_queue_ui()

    def _on_manage_queues(self):
        from my_idm.settings_dialog import SettingsDialog, TAB_QUEUES

        dialog = SettingsDialog(
            general_config=self._manager.general_config,
            torrent_config=self._manager.torrent_config,
            network_config=self._manager.network_config,
            security_config=self._manager.security_config,
            tor_config=self._manager.tor_config,
            external_tools_config=self._manager.external_tools_config,
            browser_config=self._manager.browser_config,
            db=self._manager._db,
            parent=self,
            initial_tab=TAB_QUEUES,
            manager=self._manager,
        )
        if dialog.exec():
            self._refresh_queue_ui()
            self._load_history()
            self._update_count_label()
            if hasattr(self, "_details_panel"):
                self._details_panel.refresh_queues()
        self._status_label.setText(dialog.result_message or "")

    def _on_move_selected_to_queue(self, queue_id: str):
        ids = self._selected_ids()
        if not ids:
            self._status_label.setText("Select one or more downloads first.")
            return
        moved, message = self._manager.move_downloads_to_queue(ids, queue_id)
        self._status_label.setText(message)
        if moved:
            self._load_history()

    def _on_download_removed(self, download_id: str):
        self._model.remove_entry(download_id)
        self._update_count_label()
        self._update_speed_label()
        self._update_action_states()

    def _on_download_moved(self, download_id: str):
        entry = self._manager.get_entry(download_id)
        if entry:
            self._model.refresh_entry(download_id, entry)
            self._update_action_states()

    def _on_download_renamed(self, download_id: str, new_filename: str):
        entry = self._manager.get_entry(download_id)
        if entry:
            entry.filename = new_filename
            self._model.refresh_entry(download_id, entry)
        else:
            self._model.rename_entry(download_id, new_filename)
        self._table.viewport().update()
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()
        self._update_action_states()

    def _on_download_url_updated(self, download_id: str, new_url: str):
        self._model.update_url(download_id, new_url)
        fresh = self._manager.get_entry(download_id)
        if fresh is not None:
            self._model.refresh_entry(download_id, fresh)
        self._table.viewport().update()
        if not self._details_panel.isHidden() and self._details_panel.current_download_id == download_id:
            self._details_panel.refresh()
        self._update_action_states()

    # -- helpers -------------------------------------------------------------

    def _update_count_label(self):
        total_all = self._model.total_unfiltered_count()
        visible = self._model.visible_download_count()
        active = sum(
            1 for e in self._model.all_entries
            if e.status in ("downloading", "checking", "fetching_metadata")
        )
        selected = len(self._selected_ids())
        if self._model.is_filtered():
            base = f"{visible} of {total_all} Downloads, {active} Active (Filtered)"
        else:
            base = f"{total_all} Downloads, {active} Active"
        if selected:
            self._count_label.setText(f"{base} — {selected} Selected")
        else:
            self._count_label.setText(base)

    def _update_speed_label(self):
        down, up = self._model.get_aggregate_speeds()
        from my_idm.download_model import _format_speed
        net_cfg = self._manager.network_config
        dl_lim = net_cfg.download_limit or 0
        ul_lim = net_cfg.upload_limit or 0
        dl_tag = f" [Limit: {_format_speed(dl_lim)}]" if dl_lim > 0 else ""
        ul_tag = f" [Limit: {_format_speed(ul_lim)}]" if ul_lim > 0 else ""
        self._speed_label.setText(f"↓ {_format_speed(down)}{dl_tag}  ↑ {_format_speed(up)}{ul_tag}")
        self._speed_label.setToolTip(
            f"Total Transfer Speed\n"
            f"Download Limit: {_format_speed(dl_lim) if dl_lim > 0 else 'Unlimited'}\n"
            f"Upload Limit: {_format_speed(ul_lim) if ul_lim > 0 else 'Unlimited'}\n"
            f"Click or right-click to adjust limits"
        )

    def _on_bandwidth_limits_changed(self, download_limit: int, upload_limit: int):
        self._update_speed_label()

    def _on_bandwidth_warning(self, queue_id: str, message: str, percentage: float, is_global: bool):
        """Show bandwidth warning at right edge of menubar and status bar."""
        scope = "Global" if is_global else f"Queue '{queue_id}'"
        warning_text = f"⚠️ Bandwidth Warning: {scope} at {percentage:.1f}% — {message}"
        
        if hasattr(self, "_bw_warning_btn"):
            self._bw_warning_btn.setText(f"⚠️ Bandwidth: {percentage:.0f}%")
            self._bw_warning_btn.setToolTip(f"{warning_text}\nClick to configure Bandwidth Limits.")
            self._bw_warning_btn.setStyleSheet("""
                QPushButton {
                    color: #ffb86c;
                    font-weight: bold;
                    padding: 2px 8px;
                    border: 1px solid #ffb86c;
                    border-radius: 3px;
                    background-color: rgba(255, 184, 108, 0.15);
                    margin: 2px 4px;
                }
                QPushButton:hover {
                    background-color: rgba(255, 184, 108, 0.3);
                }
            """)
            self._bw_warning_btn.setVisible(True)

        # Show in status bar temporarily
        self._status_label.setText(warning_text)
        QTimer.singleShot(10000, lambda: self._status_label.setText("Ready"))

    def _on_bandwidth_limit_exceeded(self, queue_id: str, message: str, percentage: float, is_global: bool):
        """Show bandwidth limit exceeded notification and stop downloads."""
        scope = "Global" if is_global else f"Queue '{queue_id}'"
        warning_text = f"🚫 Bandwidth Limit Exceeded: {scope} — {message}"
        
        if hasattr(self, "_bw_warning_btn"):
            self._bw_warning_btn.setText("🚫 Bandwidth (100%)")
            self._bw_warning_btn.setToolTip(f"{warning_text}\nAll downloads paused. Click to configure limits.")
            self._bw_warning_btn.setStyleSheet("""
                QPushButton {
                    color: #ff5555;
                    font-weight: bold;
                    padding: 2px 8px;
                    border: 1px solid #ff5555;
                    border-radius: 3px;
                    background-color: rgba(255, 85, 85, 0.2);
                    margin: 2px 4px;
                }
                QPushButton:hover {
                    background-color: rgba(255, 85, 85, 0.35);
                }
            """)
            self._bw_warning_btn.setVisible(True)

        # Show in status bar
        self._status_label.setText(warning_text)
        
        # Show a message box
        QMessageBox.warning(
            self,
            "Bandwidth Limit Exceeded",
            f"{warning_text}\n\nAll downloads have been paused."
        )

    def _on_bandwidth_warning_cleared(self):
        """Hide the bandwidth warning button in the menubar."""
        if hasattr(self, "_bw_warning_btn"):
            self._bw_warning_btn.setVisible(False)

    def _on_open_bandwidth_settings(self):
        """Open Preferences dialog on the Bandwidth Limit tab."""
        self._on_open_preferences(TAB_BANDWIDTH)

    def _on_open_scheduler_settings(self):
        """Open Preferences dialog on the Scheduler tab."""
        self._on_open_preferences(TAB_SCHEDULER)

    def _show_speed_context_menu(self, pos):
        menu = QMenu(self)
        from my_idm.download_model import _format_speed
        net_cfg = self._manager.network_config
        curr_dl = net_cfg.download_limit or 0
        curr_ul = net_cfg.upload_limit or 0

        # Submenu for Download Speed Limit
        dl_menu = menu.addMenu("Download Speed Limit")
        preset_values = {val for _, val in SPEED_LIMIT_PRESETS}
        for label, val in SPEED_LIMIT_PRESETS:
            act = dl_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(curr_dl == val)
            act.triggered.connect(lambda checked=False, v=val: self._set_speed_limit(v, is_upload=False))

        dl_menu.addSeparator()
        custom_dl_act = dl_menu.addAction(
            f"Custom ({_format_speed(curr_dl)})..." if (curr_dl > 0 and curr_dl not in preset_values) else "Custom..."
        )
        custom_dl_act.setCheckable(True)
        custom_dl_act.setChecked(curr_dl > 0 and curr_dl not in preset_values)
        custom_dl_act.triggered.connect(lambda: self._prompt_custom_speed_limit(is_upload=False))

        # Submenu for Upload Speed Limit
        ul_menu = menu.addMenu("Upload Speed Limit")
        for label, val in SPEED_LIMIT_PRESETS:
            act = ul_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(curr_ul == val)
            act.triggered.connect(lambda checked=False, v=val: self._set_speed_limit(v, is_upload=True))

        ul_menu.addSeparator()
        custom_ul_act = ul_menu.addAction(
            f"Custom ({_format_speed(curr_ul)})..." if (curr_ul > 0 and curr_ul not in preset_values) else "Custom..."
        )
        custom_ul_act.setCheckable(True)
        custom_ul_act.setChecked(curr_ul > 0 and curr_ul not in preset_values)
        custom_ul_act.triggered.connect(lambda: self._prompt_custom_speed_limit(is_upload=True))

        menu.exec(self._speed_label.mapToGlobal(pos))

    def _set_speed_limit(self, limit: int, is_upload: bool):
        net_cfg = self._manager.network_config
        dl = net_cfg.download_limit if is_upload else limit
        ul = limit if is_upload else net_cfg.upload_limit
        self._manager.set_bandwidth_limits(dl, ul)
        self._update_speed_label()

    def _prompt_custom_speed_limit(self, is_upload: bool):
        net_cfg = self._manager.network_config
        curr = (net_cfg.upload_limit if is_upload else net_cfg.download_limit) or 0
        kind = "Upload" if is_upload else "Download"
        val_kb, ok = QInputDialog.getInt(
            self,
            f"Custom {kind} Speed Limit",
            f"Enter {kind.lower()} speed limit in KB/s (0 = Unlimited):",
            value=curr // 1024,
            minValue=0,
            maxValue=10_000_000,
            step=10,
        )
        if ok:
            self._set_speed_limit(val_kb * 1024, is_upload)

    def _apply_persisted_theme(self) -> str:
        """Apply the theme stored in ``ui_state``, returning the id actually applied.

        An unknown or corrupt value falls back to the default rather than raising: this runs
        inside ``__init__``, so an exception here would leave no window at all.
        """
        from my_idm.styles import DEFAULT_THEME, apply_theme, normalize_theme

        try:
            stored = self._manager.db.get_ui_state("theme", DEFAULT_THEME)
        except Exception:
            log.warning("Could not read the persisted theme", exc_info=True)
            stored = DEFAULT_THEME
        return apply_theme(QApplication.instance(), normalize_theme(stored))

    def _on_theme_applied(self, theme: str) -> None:
        """React to the Preferences ▸ Views theme selector applying a theme.

        ``styles.apply_theme`` has already set the application stylesheet, so the only thing
        left is anything this window caches in palette colours.
        """
        # A theme can change the font metrics the toolbar row is measured against.
        self._fit_min_width_to_toolbar()
        self._save_ui_state_to_db()

    def _on_show_statistics(self):
        """Raise the statistics popup, beside Preferences in the toolbar, or open it.

        The speed sparkline reads the same aggregate the status bar shows, so the chart and
        the status bar can never disagree - see ``docs/architecture/statistics.md``.

        One popup at a time: it is modeless (``show()``, not ``exec()``) and nothing
        ``WA_DeleteOnClose``s it, so without this guard every toolbar click would leave
        another live dialog - each with its own widgets and 1 Hz QTimer - on screen for
        the rest of the session, and the user would have to hunt for the one they wanted.
        """
        from my_idm.stats_dialog import StatisticsPopup

        existing = self._stats_dialog
        if existing is not None:
            try:
                if existing.isVisible():
                    existing.raise_()
                    existing.activateWindow()
                    return existing
            except RuntimeError:
                # The C++ side is already gone (closed and destroyed); fall through and
                # build a fresh one rather than raising into a dangling wrapper.
                self._stats_dialog = None

        dlg = StatisticsPopup(
            self._manager._db,
            parent=self,
            speed_provider=lambda: self._model.get_aggregate_speeds()[0],
        )
        # Qt deletes the dialog on close, and we drop our reference at the same time, so a
        # session's worth of toolbar clicks cannot pile up hidden dialogs and their timers.
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._stats_dialog = dlg
        dlg.finished.connect(self._on_statistics_closed)
        dlg.show()
        return dlg

    def _on_statistics_closed(self, _result: int) -> None:
        """Drop our reference once the popup is gone, so the next click rebuilds it.

        ``finished`` fires before the ``WA_DeleteOnClose`` teardown, and again for the
        modal path's ``accept``/``reject``, so either way the next click starts clean.
        """
        self._stats_dialog = None

    def _on_open_preferences(self, initial_tab=TAB_GENERAL):
        """Open Preferences, optionally on a named page (``TAB_*``).

        Pass the name rather than an index. ``SettingsDialog`` clamps an out-of-range
        integer away and opens the wrong page without a word, which is how six Tools-menu
        items ended up pointing one or two pages off after the 6 -> 9 tab split.
        """
        dlg = SettingsDialog(
            general_config=self._manager.general_config,
            torrent_config=self._manager.torrent_config,
            network_config=self._manager.network_config,
            security_config=self._manager.security_config,
            tor_config=self._manager.tor_config,
            external_tools_config=self._manager.external_tools_config,
            browser_config=self._manager.browser_config,
            db=self._manager._db,
            parent=self,
            initial_tab=initial_tab,
        )
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self._manager.set_general_config(dlg.general_config)
            self._manager.set_torrent_config(dlg.torrent_config)
            self._manager.set_network_config(dlg.network_config)
            self._manager.set_security_config(dlg.security_config)
            self._manager.set_external_tools_config(dlg.external_tools_config)
            self._manager.set_browser_config(dlg.browser_config)
            old_tor_enabled = self._manager.tor_config.enabled
            self._manager.set_tor_config(dlg.tor_config)
            if dlg.tor_config.enabled != old_tor_enabled:
                self._on_toggle_tor(dlg.tor_config.enabled)
            if self._tray_icon:
                if dlg.general_config.enable_system_tray:
                    self._tray_icon.show()
                else:
                    self._tray_icon.hide()
            # The Download via AnimePahe button no longer pops a confirm dialog:
            # it closes Preferences and drops the user into the live console, which
            # is where the scraper's progress is visible.
            if getattr(dlg, "animepahe_download_started", False):
                if not self._details_panel.isVisible():
                    self._details_panel.setVisible(True)
                    self._act_toggle_details.setChecked(True)
                self._details_panel.set_mode("console")
                self._act_toggle_details.setChecked(True)

    def _on_open_torrent_settings(self):
        self._on_open_preferences(TAB_TORRENT)

    def _on_open_browser_settings(self):
        self._on_open_preferences(TAB_BROWSER)

    def _on_open_network_settings(self):
        self._on_open_preferences(TAB_VPN)

    def _on_open_tor_settings(self):
        self._on_open_preferences(TAB_TOR)

    def _on_open_security_settings(self):
        self._on_open_preferences(TAB_SECURITY)

    def _on_open_external_tools_settings(self):
        self._on_open_preferences(TAB_EXTERNAL_TOOLS)

    def _on_launch_animepahe_gui(self):
        cfg = self._manager.external_tools_config
        repo = cfg.get_effective_repo_path()
        if not repo or not os.path.isdir(repo):
            res = QMessageBox.question(
                self,
                "AnimePahe Not Configured",
                "The AnimePahe repository folder is not configured or does not exist.\n\n"
                "Would you like to configure the repository location in Preferences now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if res == QMessageBox.StandardButton.Yes:
                self._on_open_external_tools_settings()
            return

        ok, msg = launch_animepahe_gui(cfg)
        if ok:
            self._status_label.setText("Launched AnimePahe Downloader GUI")
        else:
            QMessageBox.warning(self, "Failed to Launch AnimePahe GUI", msg)

    def _on_start_animepahe_cli(self):
        cfg = self._manager.external_tools_config
        repo = cfg.get_effective_repo_path()
        if not repo or not os.path.isdir(repo):
            res = QMessageBox.question(
                self,
                "AnimePahe Not Configured",
                "The AnimePahe repository folder is not configured or does not exist.\n\n"
                "Would you like to configure the repository location in Preferences now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if res == QMessageBox.StandardButton.Yes:
                self._on_open_external_tools_settings()
            return

        ok, msg = self._manager.start_animepahe_scraper()
        self._status_label.setText(msg)
        if not ok:
            QMessageBox.warning(self, "Failed to Start Scraper", msg)

    def _on_toggle_details_btn_clicked(self):
        if self._details_panel.isVisible() and self._details_panel.current_mode() == "details":
            self._act_toggle_details.setChecked(False)
        else:
            self._details_panel.set_mode("details")
            self._act_toggle_details.setChecked(True)

    def _on_toggle_console_btn_clicked(self):
        if self._details_panel.isVisible() and self._details_panel.current_mode() == "console":
            self._act_toggle_details.setChecked(False)
        else:
            self._details_panel.set_mode("console")
            self._act_toggle_details.setChecked(True)

    def _on_view_animepahe_console_log(self):
        self._on_toggle_console_btn_clicked()

    def _footer_toggle_style(self, active: bool) -> str:
        if active:
            return """
                QPushButton {
                    background: rgba(88, 166, 255, 0.15);
                    color: #58a6ff;
                    border: 1px solid #58a6ff;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: rgba(88, 166, 255, 0.25);
                    border-color: #79c0ff;
                    color: #79c0ff;
                }
            """
        return """
            QPushButton {
                background: transparent;
                color: #8892b0;
                border: 1px solid #3b4252;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
            }
            QPushButton:hover {
                background: #2e3440;
                color: #d8dee9;
            }
        """

    def _sync_panel_buttons(self):
        is_vis = getattr(self, "_act_toggle_details", None) is not None and self._act_toggle_details.isChecked() and not self._details_panel.isHidden()
        if hasattr(self, "_details_status_btn"):
            self._details_status_btn.setText("📋 Details: ON" if is_vis else "📋 Details: OFF")
            self._details_status_btn.setStyleSheet(self._footer_toggle_style(is_vis))

    def _on_open_animepahe_console_log_file(self):
        from my_idm.external_tools import open_file_in_default_app
        log_path = self._manager.external_tools_config.get_console_log_path()
        ok, msg = open_file_in_default_app(log_path, create_if_missing=True)
        if not ok:
            QMessageBox.warning(self, "Cannot Open Console Log", msg)

    def _on_view_animepahe_debug_log(self):
        from my_idm.external_tools import open_file_in_default_app
        log_path = self._manager.external_tools_config.get_debug_log_path()
        ok, msg = open_file_in_default_app(log_path, create_if_missing=True)
        if not ok:
            QMessageBox.warning(self, "Cannot Open Debug Log", msg)

    def _on_animepahe_status_changed(self, is_running: bool):
        self._animepahe_status_btn.setVisible(is_running)
        if is_running:
            q_len = getattr(self._manager, "get_animepahe_queue_length", lambda: 0)()
            if q_len > 0:
                self._animepahe_status_btn.setText(f"🎬 AnimePahe: Active (+{q_len} queued)")
            else:
                self._animepahe_status_btn.setText("🎬 AnimePahe: Active")

    def _on_animepahe_queue_changed(self, queue_len: int):
        if self._animepahe_status_btn.isVisible():
            if queue_len > 0:
                self._animepahe_status_btn.setText(f"🎬 AnimePahe: Active (+{queue_len} queued)")
            else:
                self._animepahe_status_btn.setText("🎬 AnimePahe: Active")

    def _show_animepahe_status_menu(self):
        menu = QMenu(self)

        act_console = QAction(_create_emoji_icon("📄"), "View Console Logs (Bottom Panel)", self)
        act_console.triggered.connect(self._on_view_animepahe_console_log)
        menu.addAction(act_console)

        act_file = QAction(_create_emoji_icon("↗️"), "Open Console Log in Editor…", self)
        act_file.triggered.connect(self._on_open_animepahe_console_log_file)
        menu.addAction(act_file)

        act_debug = QAction(_create_emoji_icon("🔍"), "View Debug Logs…", self)
        act_debug.triggered.connect(self._on_view_animepahe_debug_log)
        menu.addAction(act_debug)

        menu.addSeparator()

        act_gui = QAction(_create_emoji_icon("🎬"), "Launch AnimePahe GUI…", self)
        act_gui.triggered.connect(self._on_launch_animepahe_gui)
        menu.addAction(act_gui)

        if self._manager.is_animepahe_running():
            q_len = getattr(self._manager, "get_animepahe_queue_length", lambda: 0)()
            if q_len > 0:
                act_info = QAction(f"📋 Pending in Queue: {q_len} task(s)", self)
                act_info.setEnabled(False)
                menu.addAction(act_info)

            stop_label = f"Stop Scraper & Clear Queue ({q_len})" if q_len > 0 else "Stop Background Scraper"
            act_stop = QAction(_create_emoji_icon("⏹️"), stop_label, self)
            def _stop():
                ok, msg = self._manager.stop_animepahe_scraper()
                self._status_label.setText(msg)
            act_stop.triggered.connect(_stop)
            menu.addAction(act_stop)
        else:
            act_start = QAction(_create_emoji_icon("▶️"), "Start Background Scraper (CLI)", self)
            act_start.triggered.connect(self._on_start_animepahe_cli)
            menu.addAction(act_start)

        menu.addSeparator()
        act_settings = QAction(_create_emoji_icon("🛠️"), "External Tools Settings…", self)
        act_settings.triggered.connect(self._on_open_external_tools_settings)
        menu.addAction(act_settings)

        btn_pos = self._animepahe_status_btn.mapToGlobal(QPoint(0, -menu.sizeHint().height()))
        menu.exec(btn_pos)

    def _on_scan_selected_file(self):
        for did in self._selected_ids():
            self._manager.scan_download_file(did)

    def _on_threat_detected(self, download_id: str, report: str):
        entry = self._manager.get_entry(download_id)
        name = entry.filename if entry else download_id
        QMessageBox.critical(
            self,
            "⚠️ Malware / Threat Detected!",
            f"Antivirus scanning detected a potential threat in:\n\n"
            f"File: {name}\n\n"
            f"Details:\n{report}",
        )

    def _update_network_status_badge(self, config: NetworkConfig):
        if config.is_interface_bound:
            is_vpn = is_vpn_adapter_name(config.interface_name)
            icon = "🛡️ VPN" if is_vpn else "🌐 Net"
            ks = " [🔒 KS]" if config.kill_switch else ""
            self._vpn_status_btn.setText(f"{icon}: {config.interface_name}{ks}")
            self._vpn_status_btn.setToolTip(
                f"Bound to {config.interface_name} ({config.interface_ip})\n"
                f"Kill Switch: {'Enabled' if config.kill_switch else 'Disabled'}\n"
                "Click to configure VPN & Network Settings"
            )
            bg = "#193524" if is_vpn else "#222d3d"
            fg = "#50fa7b" if is_vpn else "#8be9fd"
            border = "#50fa7b" if is_vpn else "#3d4b60"
            self._vpn_status_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    color: {fg};
                    border: 1px solid {border};
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background: #2a4030;
                }}
            """)
        elif config.proxy_enabled and config.proxy_host:
            self._vpn_status_btn.setText(f"🌐 Proxy: {config.proxy_type.upper()}")
            self._vpn_status_btn.setToolTip(
                f"Proxy: {config.proxy_url}\nClick to configure VPN & Network Settings"
            )
            self._vpn_status_btn.setStyleSheet("""
                QPushButton {
                    background: #222d3d;
                    color: #f1fa8c;
                    border: 1px solid #6272a4;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: #2d3b50;
                }
            """)
        else:
            self._vpn_status_btn.setText("🌐 Net: Default")
            self._vpn_status_btn.setToolTip(
                "Using system default routing.\nClick to configure VPN & Network Settings"
            )
            self._vpn_status_btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #8892b0;
                    border: 1px solid #3b4252;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background: #2e3440;
                    color: #d8dee9;
                }
            """)

    def _on_toggle_tor(self, checked: bool):
        self._on_tor_status_changed("connecting" if checked else "disconnecting", "")
        QApplication.processEvents()
        success, msg = self._manager.toggle_tor(checked)
        self._status_label.setText(msg)
        self._model.set_tor_config(self._manager.tor_config)
        self._table.viewport().update()
        if not success and checked:
            self._act_tor.blockSignals(True)
            self._act_tor.setChecked(False)
            self._act_tor.blockSignals(False)
            self._on_tor_status_changed("error", msg)

            QMessageBox.critical(
                self,
                "⚠️ Tor Connection Error",
                f"Unable to activate Tor network privacy:\n\n{msg}\n\n"
                "Please verify that Tor or Tor Browser is installed, or configure the path in Tools → Tor Network Settings.",
            )
        else:
            self._on_tor_status_changed("connected" if checked else "disconnected", msg)

    def _on_search_changed(self, text: str) -> None:
        """Filter the table from the toolbar search box."""
        self._model.set_search_query(text)
        self._update_count_label()
        self._update_speed_label()
        self._apply_table_spans()

    def _clear_search(self) -> None:
        if self._search_edit.text():
            self._search_edit.clear()

    def _on_tor_availability_changed(self, available: bool):
        """Tor came up or went down: refresh rows that display a Tor route."""
        self._table.viewport().update()
        if not self._details_panel.isHidden():
            if getattr(self._details_panel, "current_download_id", None):
                self._details_panel.refresh()

    def _on_tor_status_changed(self, state: str, message: str):
        if state == "connecting":
            self._tor_toolbar_progress.setVisible(True)
            self._tor_toolbar_progress.setRange(0, 0)
            self._tor_footer_progress.setVisible(True)
            self._tor_footer_progress.setRange(0, 0)
            self._tor_status_btn.setText("🧅 Tor: Connecting...")
            self._act_tor.setText("Tor: Connecting...")
            self._apply_tor_transition_style()
        elif state == "disconnecting":
            self._tor_toolbar_progress.setVisible(True)
            self._tor_toolbar_progress.setRange(0, 0)
            self._tor_footer_progress.setVisible(True)
            self._tor_footer_progress.setRange(0, 0)
            self._tor_status_btn.setText("🧅 Tor: Disconnecting...")
            self._act_tor.setText("Tor: Disconnecting...")
            self._apply_tor_transition_style()
        elif state in ("connected", "disconnected", "error"):
            self._tor_toolbar_progress.setVisible(False)
            self._tor_footer_progress.setVisible(False)
            self._on_tor_config_changed(self._manager.tor_config)

    def _apply_tor_transition_style(self):
        trans_btn_style = """
            QPushButton {
                background: #2a2818;
                color: #f1fa8c;
                border: 1px solid #f1fa8c;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #383520;
            }
        """
        self._tor_status_btn.setStyleSheet(trans_btn_style)

        trans_tb_style = """
            QToolButton {
                background: #2a2818;
                color: #f1fa8c;
                border: 1px solid #f1fa8c;
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 12px;
                font-weight: bold;
            }
            QToolButton:hover {
                background: #383520;
            }
        """
        if hasattr(self, "_tor_toolbar_btn"):
            self._tor_toolbar_btn.setStyleSheet(trans_tb_style)

    def _on_tor_config_changed(self, config: TorConfig):
        self._model.set_tor_config(config)
        self._table.viewport().update()

        self._act_tor.blockSignals(True)
        self._act_tor.setChecked(config.enabled)
        self._act_tor.blockSignals(False)

        self._tor_toolbar_progress.setVisible(False)
        self._tor_footer_progress.setVisible(False)

        if config.enabled:
            self._act_tor.setText("Tor: ON")
            routed = []
            if config.route_http:
                routed.append("HTTP")
            if config.route_torrent:
                routed.append("Torrent")
            traffic_str = ", ".join(routed) if routed else "None"
            self._act_tor.setToolTip(
                f"Tor is ON ({config.socks5_url})\n"
                f"Routing: {traffic_str}\n"
                "Click to disable Tor routing"
            )
            self._tor_status_btn.setText(f"🧅 Tor: {traffic_str}")
            self._tor_status_btn.setToolTip(
                f"Tor active on {config.proxy_host}:{config.proxy_port}\n"
                f"Traffic routed: {traffic_str}\n"
                "Click to open Tor Settings"
            )
            # Green styling when ON
            green_btn_style = """
                QPushButton {
                    background: #193524;
                    color: #50fa7b;
                    border: 1px solid #50fa7b;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: #2a4e34;
                }
            """
            self._tor_status_btn.setStyleSheet(green_btn_style)

            green_tb_style = """
                QToolButton {
                    background: #193524;
                    color: #50fa7b;
                    border: 1px solid #50fa7b;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 12px;
                    font-weight: bold;
                }
                QToolButton:hover {
                    background: #2a4e34;
                }
            """
            if hasattr(self, "_tor_toolbar_btn"):
                self._tor_toolbar_btn.setStyleSheet(green_tb_style)
        else:
            self._act_tor.setText("Tor: OFF")
            self._act_tor.setToolTip(
                "Tor is OFF\nClick to enable Tor routing"
            )
            self._tor_status_btn.setText("🧅 Tor: OFF")
            self._tor_status_btn.setToolTip(
                "Tor is disabled.\nClick to open Tor Settings"
            )
            off_btn_style = """
                QPushButton {
                    background: transparent;
                    color: #6272a4;
                    border: 1px solid #3b4252;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background: #2e3440;
                    color: #d8dee9;
                }
            """
            self._tor_status_btn.setStyleSheet(off_btn_style)

            off_tb_style = """
                QToolButton {
                    background: transparent;
                    color: #8892b0;
                    border: 1px solid #3b4252;
                    border-radius: 4px;
                    padding: 2px 8px;
                    font-size: 12px;
                }
                QToolButton:hover {
                    background: #2e3440;
                    color: #d8dee9;
                }
            """
            if hasattr(self, "_tor_toolbar_btn"):
                self._tor_toolbar_btn.setStyleSheet(off_tb_style)

    # -- details panel handlers ----------------------------------------------

    def _on_toggle_details(self, checked: bool):
        if checked:
            self._details_panel.setVisible(True)
            sizes = self._splitter.sizes()
            total = sum(sizes) if sum(sizes) > 250 else (self.height() or 700)
            bot_h = min(total - 100, max(140, getattr(self, "_details_height", 250)))
            top_h = max(100, total - bot_h)
            self._splitter.setSizes([top_h, bot_h])
            entry = self._first_selected_entry()
            self._details_panel.set_download_id(entry.id if entry else None)
        else:
            sizes = self._splitter.sizes()
            if len(sizes) == 2 and sizes[1] >= 50:
                self._details_height = sizes[1]
            self._details_panel.setVisible(False)
        tip = (
            "Hide bottom panel (F4)"
            if checked
            else "Show bottom panel (F4)"
        )
        self._act_toggle_details.setToolTip(tip)
        self._sync_panel_buttons()

    def _on_reset_view(self):
        """Reset all table/grid settings to defaults: column visibility, filters, sorting, and column widths."""
        # 1. Reset column widths to defaults
        _apply_default_column_widths(self._table)

        # 2. Show all columns (reset column visibility)
        header = self._header_view
        for col in range(Col.COUNT):
            header.setSectionHidden(col, False)

        # 3. Reset column order to default (see _DEFAULT_COLUMN_ORDER)
        # Ascending order is required: moving each logical index into place in
        # sequence restores true identity order. A descending sweep leaves the
        # displaced section stranded near the front, which then shifts every
        # subsequent position.
        for logical in range(Col.COUNT):
            visual = header.visualIndex(logical)
            if visual != logical:
                header.moveSection(visual, logical)
        _apply_default_column_order(header)

        # 4. Clear all filters (status and type filters)
        self._model.clear_filters()

        # 5. Reset sorting to default: Date Added, Descending
        self._table.sortByColumn(Col.ADDED, Qt.SortOrder.DescendingOrder)
        self._last_sort_section = Col.ADDED

        # 6. Reset row height to default
        self._table.verticalHeader().setDefaultSectionSize(36)

        # 7. Update UI state in database
        self._save_ui_state_to_db()

        self._status_label.setText("View reset to defaults")

    def _on_table_selection_changed(self, *args):
        entry = self._first_selected_entry()
        self._details_panel.set_download_id(entry.id if entry else None)
        self._update_queue_status(entry)
        self._update_action_states()
        self._update_count_label()

    def _update_queue_status(self, entry):
        """Show which queue the selected download belongs to.

        A queue is otherwise invisible in the list: nothing in the row says where it lives,
        so the only way to find out was to open the row's context menu and read the checkmark
        in **Move to Queue**. With a single row selected this puts it in plain sight; with
        several, the queues are summarised, since a per-row answer would not fit.
        """
        if entry is None:
            self._queue_status_label.setText("")
            return
        name = self._queue_display_name(entry.queue_id)
        selected = len(self._selected_ids())
        if selected > 1:
            queues = sorted({
                self._queue_display_name(e.queue_id)
                for e in self._manager.get_all_entries()
                if e.id in set(self._selected_ids())
            })
            if len(queues) == 1:
                self._queue_status_label.setText(f"🗃️ {queues[0]}")
            else:
                self._queue_status_label.setText(f"🗃️ {len(queues)} queues")
            return
        self._queue_status_label.setText(f"🗃️ {name}")

    def _queue_display_name(self, queue_id: str) -> str:
        """The queue's name, or Default for a blank or dangling id."""
        queue = self._manager.get_queue(queue_id)
        return queue.name if queue else DEFAULT_QUEUE_NAME

    def _on_details_timer_tick(self):
        # Polled here rather than on its own timer: this one already runs at 1 Hz on the GUI
        # thread, and it keeps running while the window is hidden to tray - which is exactly
        # when a midnight rollover happens unnoticed.
        if self._model.date_grouping_is_stale():
            self._regroup_for_new_day()
        if self._details_panel.isVisible():
            if self._details_panel.current_mode() == "queues":
                self._details_panel.refresh_queues()
            elif self._details_panel.current_download_id:
                self._details_panel.refresh()
        self._update_speed_label()

    def _regroup_for_new_day(self):
        """Regroup the date sections after local midnight, keeping the selection.

        Today/Yesterday/Last 7 Days are relative to the current day, so a window left open
        across midnight kept yesterday's grouping until some unrelated event happened to
        rebuild the model. The rebuild is a model reset, which drops the view's selection, so
        the ids are captured first and reapplied exactly as _on_toggle_segregated_view does.
        _apply_table_spans is already wired to modelReset; calling it here keeps this handler
        self-contained and consistent with every other caller that mutates the sections.
        """
        selected = self._selected_ids()
        if not self._model.refresh_date_grouping():
            return
        self._restore_selection(selected)
        self._apply_table_spans()
        if self._segregated_view_enabled:
            self._status_label.setText("Day grouping updated")

    # -- UI State persistence in database ------------------------------------

    def _save_ui_state_to_db(self):
        """Save window size, location, maximized or not, column lengths, splitter state in DB."""
        try:
            is_max = self.isMaximized()
            # If currently maximized, normalGeometry gives the un-maximized rect for restoring size & location
            geom = self.normalGeometry() if is_max else self.geometry()

            col_widths = {
                str(col): self._table.columnWidth(col)
                for col in range(Col.COUNT)
            }
            header_hex = bytes(self._table.horizontalHeader().saveState().toHex()).decode()
            splitter_hex = bytes(self._splitter.saveState().toHex()).decode()
            sizes = self._splitter.sizes()
            # Use the toggle's checked state, not isVisible(): every child widget
            # reports isVisible() == False while the window is hidden or
            # minimised (e.g. closed to tray), which would persist "panel closed"
            # even when the user had it open.
            details_vis = self._act_toggle_details.isChecked()

            if details_vis and len(sizes) == 2 and sizes[1] >= 50:
                self._details_height = sizes[1]

            total = sum(sizes) if sum(sizes) > 250 else 700
            safe_bot = min(total - 100, max(140, getattr(self, "_details_height", 250)))
            splitter_sizes = [max(100, total - safe_bot), safe_bot]

            details_state = self._details_panel.get_state()

            sort_sec = self._table.horizontalHeader().sortIndicatorSection()
            try:
                sort_ord = int(self._table.horizontalHeader().sortIndicatorOrder().value)
            except (AttributeError, TypeError, ValueError):
                sort_ord = int(Qt.SortOrder.DescendingOrder.value)

            if sort_sec < 0:
                sort_sec = Col.ADDED
                sort_ord = int(Qt.SortOrder.DescendingOrder.value)

            window_geom_hex = bytes(self.saveGeometry().toHex()).decode()

            state = {
                "window_geometry": window_geom_hex,
                "x": geom.x() if is_max else self.x(),
                "y": geom.y() if is_max else self.y(),
                "width": geom.width(),
                "height": geom.height(),
                "is_maximized": is_max,
                "column_widths": col_widths,
                "header_state": header_hex,
                # Recorded so a state written by an older build (fewer columns)
                # can be recognised on restore and healed instead of scrambling
                # the order of any columns added since.
                "column_count": Col.COUNT,
                "splitter_state": splitter_hex,
                "splitter_sizes": splitter_sizes,
                "details_visible": details_vis,
                "details_height": safe_bot,
                "details_state": details_state,
                "sort_column": sort_sec,
                "sort_order": sort_ord,
            }
            self._manager.save_ui_state(state)

            # Also sync to QSettings for backwards compatibility
            settings = QSettings("MyIDM", "My-IDM")
            settings.setValue("header_state", self._table.horizontalHeader().saveState())
            settings.setValue("splitter_state", self._splitter.saveState())
            settings.setValue("details_visible", details_vis)
            settings.setValue("details_height", safe_bot)
            settings.setValue("details_tab", details_state.get("current_tab", 0))
        except Exception as exc:
            log.warning("Failed to save window state to DB: %s", exc)

    def _restore_ui_state_from_db(self):
        """Restore window size, location, maximized or not, column lengths, splitter state from DB."""
        try:
            state = self._manager.get_ui_state()
            if not state:
                # Fallback to legacy QSettings if DB has no stored state
                settings = QSettings("MyIDM", "My-IDM")
                header_state = settings.value("header_state")
                if header_state:
                    self._table.horizontalHeader().restoreState(header_state)
                else:
                    # Genuinely fresh profile: nothing saved anywhere, so start from the
                    # default arrangement. Only on this path - a user who has already chosen
                    # their columns must not have them overridden because a default changed.
                    # This branch returns early, so it repeats what the saved-state path
                    # below ends with: header flags, default sort, and - in the `finally` -
                    # the minimum section size.
                    for col in DEFAULT_HIDDEN_COLUMNS:
                        self._table.horizontalHeader().setSectionHidden(col, True)
                splitter_state = settings.value("splitter_state")
                if splitter_state:
                    self._splitter.restoreState(splitter_state)

                details_h = settings.value("details_height")
                if details_h is not None:
                    try:
                        self._details_height = max(140, int(details_h))
                    except (ValueError, TypeError):
                        self._details_height = 250
                else:
                    self._details_height = 250

                details_tab = settings.value("details_tab")
                if details_tab is not None:
                    self._details_panel.restore_state({"current_tab": details_tab})

                details_vis = settings.value("details_visible")
                if details_vis is not None:
                    is_vis = bool(details_vis)
                else:
                    is_vis = True

                self._act_toggle_details.setChecked(is_vis)
                self._details_panel.setVisible(is_vis)

                sizes = self._splitter.sizes()
                total = sum(sizes) if sum(sizes) > 250 else 700
                safe_bot = min(total - 100, max(140, self._details_height))
                safe_top = max(100, total - safe_bot)
                if is_vis:
                    self._splitter.setSizes([safe_top, safe_bot])
                else:
                    self._splitter.setSizes([total, 0])

                tip = (
                    "Hide bottom panel (F4)"
                    if is_vis
                    else "Show bottom panel (F4)"
                )
                self._act_toggle_details.setToolTip(tip)
                self._sync_panel_buttons()

                self._table.horizontalHeader().setSectionsMovable(True)
                self._table.horizontalHeader().setFirstSectionMovable(True)
                self._table.horizontalHeader().setStretchLastSection(False)
                self._table.horizontalHeader().setCascadingSectionResizes(False)
                self._table.sortByColumn(Col.ADDED, Qt.SortOrder.DescendingOrder)
                self._last_sort_section = Col.ADDED
                return

            # Window size & location
            window_geom = state.get("window_geometry")
            if window_geom:
                try:
                    self.restoreGeometry(QByteArray.fromHex(window_geom.encode()))
                except Exception:
                    pass
            else:
                w = state.get("width")
                h = state.get("height")
                x = state.get("x")
                y = state.get("y")
                has_valid_size = w and h and w >= 400 and h >= 300
                has_valid_pos = x is not None and y is not None
                if has_valid_size:
                    self.resize(int(w), int(h))
                if has_valid_pos:
                    self.move(int(x), int(y))
                if state.get("is_maximized"):
                    self.showMaximized()

            # Column lengths / widths
            col_widths = state.get("column_widths")
            if col_widths and isinstance(col_widths, dict):
                for col_str, width in col_widths.items():
                    try:
                        self._table.setColumnWidth(int(col_str), int(width))
                    except (ValueError, TypeError):
                        pass

            # Header state (order, sorting indicator)
            header_hex = state.get("header_state")
            if header_hex:
                try:
                    self._table.horizontalHeader().restoreState(
                        QByteArray.fromHex(header_hex.encode())
                    )
                except Exception:
                    pass
                if self._table.columnWidth(Col.QUEUE) < _DEFAULT_COLUMN_WIDTHS[Col.QUEUE]:
                    self._table.setColumnWidth(Col.QUEUE, _DEFAULT_COLUMN_WIDTHS[Col.QUEUE])
            else:
                # No saved header state: a fresh profile. Apply the default hidden set here
                # too, for the case where the DB holds window geometry from an older build
                # but no header state.
                for col in DEFAULT_HIDDEN_COLUMNS:
                    self._table.horizontalHeader().setSectionHidden(col, True)

            # Ensure sections remain movable and flags are not overwritten by saved state
            header = self._table.horizontalHeader()
            header.setSectionsMovable(True)
            header.setFirstSectionMovable(True)
            header.setStretchLastSection(False)
            header.setCascadingSectionResizes(False)

            # A state saved before columns were appended only describes the older
            # sections, so restoreState() drops the new ones wherever it likes.
            # Re-pin the tail so appended columns land at the end while the
            # user's own ordering of the older ones is preserved. The full default
            # order is deliberately NOT applied here: this runs on an existing
            # profile, and reordering everything would throw away an arrangement the
            # user never asked to have changed.
            saved_count = state.get("column_count")
            if saved_count is not None and int(saved_count) != Col.COUNT:
                _apply_default_tail_order(header)

            # Ensure Col.FILE_NAME is visible and properly sized if restored from an older state
            header.setSectionHidden(Col.FILE_NAME, False)
            if self._table.columnWidth(Col.FILE_NAME) < 50:
                self._table.setColumnWidth(
                    Col.FILE_NAME, _DEFAULT_COLUMN_WIDTHS[Col.FILE_NAME]
                )

            # Restore sort column & order: default to Col.ADDED, DescendingOrder
            sort_col = state.get("sort_column")
            sort_ord = state.get("sort_order")
            if sort_col is None or sort_col < 0:
                sort_col = Col.ADDED
                sort_ord = int(Qt.SortOrder.DescendingOrder.value)
            try:
                order = Qt.SortOrder(sort_ord)
            except (ValueError, TypeError):
                order = Qt.SortOrder.DescendingOrder
            self._table.sortByColumn(int(sort_col), order)
            self._last_sort_section = int(sort_col)

            # Splitter state & sizes
            splitter_hex = state.get("splitter_state")
            if splitter_hex:
                try:
                    self._splitter.restoreState(
                        QByteArray.fromHex(splitter_hex.encode())
                    )
                except Exception:
                    pass

            # Details height & state
            details_h = state.get("details_height")
            if details_h is not None:
                try:
                    self._details_height = max(140, int(details_h))
                except (ValueError, TypeError):
                    self._details_height = 250
                details_vis = state.get("details_visible", True)
            else:
                splitter_sizes = state.get("splitter_sizes")
                if splitter_sizes and isinstance(splitter_sizes, list) and len(splitter_sizes) == 2 and int(splitter_sizes[1]) >= 50:
                    self._details_height = max(140, int(splitter_sizes[1]))
                    details_vis = state.get("details_visible", True)
                else:
                    self._details_height = 250
                    # Auto-heal vanished legacy state where collapsed to 0
                    details_vis = True

            details_state = state.get("details_state")
            if details_state and isinstance(details_state, dict):
                self._details_panel.restore_state(details_state)

            details_vis = bool(details_vis)
            self._act_toggle_details.setChecked(details_vis)
            self._details_panel.setVisible(details_vis)

            sizes = self._splitter.sizes()
            total = sum(sizes) if sum(sizes) > 250 else (self.height() or 700)
            safe_bot = min(total - 100, max(140, self._details_height))
            safe_top = max(100, total - safe_bot)

            if details_vis:
                self._splitter.setSizes([safe_top, safe_bot])
            else:
                self._splitter.setSizes([total, 0])

            tip = (
                "Hide bottom panel (F4)"
                if details_vis
                else "Show bottom panel (F4)"
            )
            self._act_toggle_details.setToolTip(tip)
            self._sync_panel_buttons()
        except Exception as exc:
            log.warning("Failed to restore window state from DB: %s", exc)
        finally:
            # On every exit path, including the fresh-profile `return` above:
            # `restoreState()` carries the header's section floor in its blob and puts
            # Qt's font-derived default back, undoing whatever was set before it.
            self._apply_minimum_section_size()

    def _apply_minimum_section_size(self):
        """Re-assert how narrow a downloads-table column may be, after a header restore.

        Qt saves ``minimumSectionSize`` inside the header state blob, so restoring one
        reinstates the font-derived floor — 32px at the default UI font, and wider at a
        larger scale factor. That silently clamps the two narrowest columns this app ships,
        ``#`` (a row number, and the only column with no filter button to make room for) and
        ``Queue``, whose defaults are 30px. Re-applying it here is the difference between a
        default width that is honoured and one that is quietly 2px off forever.
        """
        self._table.horizontalHeader().setMinimumSectionSize(MIN_COLUMN_WIDTH)

    def changeEvent(self, event: QEvent):
        if event.type() == QEvent.Type.WindowStateChange:
            cfg = self._manager.general_config
            if (
                self.isMinimized()
                and cfg.enable_system_tray
                and cfg.minimize_to_tray
                and self._has_tray_icon()
            ):
                # Same trap as close-to-tray: with no tray icon there is nothing to minimize
                # *to*, so hiding here would strand the window with no way back.
                QTimer.singleShot(0, self.hide)
        super().changeEvent(event)
        self._update_manager_window_visibility()

    def showEvent(self, event):
        super().showEvent(event)
        # Arm .torrent drag-and-drop here rather than in __init__. `setAcceptDrops` is what makes
        # one window-level handler cover the whole window: Qt delivers a drag to the widget under
        # the cursor and propagates it up the parent chain until something accepts, and nothing
        # in this window's children accepts, so it reaches these handlers.
        #
        # First show, not construction, on purpose. Registering a drop target on Windows binds an
        # OLE registration to the window's HWND, and doing that from __init__ — before the native
        # handle exists — made short-lived windows (the test harness builds and destroys dozens)
        # leave that registration behind, which surfaced much later as an access violation inside
        # an unrelated QWidget.show(). Deferring it means a window that is never shown never
        # registers, and idempotence is free because setAcceptDrops ignores a repeated True.
        if not self.acceptDrops():
            self.setAcceptDrops(True)
        # Re-measured once the widgets are polished: at construction time the toolbar's
        # sizeHint under-reports (970px vs 1025px here, before the fonts are resolved), which
        # is exactly the overflow this prevents. The call only ever raises the minimum, so
        # repeating it is free.
        self._fit_min_width_to_toolbar()
        self._update_tray_menu_text()
        self._update_manager_window_visibility()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._update_tray_menu_text()
        self._update_manager_window_visibility()

    def _update_manager_window_visibility(self):
        """Update DownloadManager with current window visibility state for AnimePahe embedding."""
        if hasattr(self, "_manager") and self._manager:
            # Window is usable for embedding only when visible and not minimized
            visible = self.isVisible() and not self.isMinimized()
            self._manager.set_window_visible(visible)

    def _has_tray_icon(self) -> bool:
        """Whether a tray icon actually exists to hide into.

        ``_setup_system_tray`` leaves ``_tray_icon`` as ``None`` whenever
        ``QSystemTrayIcon.isSystemTrayAvailable()`` is False - the normal case on GNOME, and on
        Wayland compositors with no AppIndicator implementation - so the ``enable_system_tray``
        preference can be on while there is nothing behind it. Anything that hides the window must
        consult this, not the preference.
        """
        return self._tray_icon is not None

    def _can_hide_to_tray(self) -> bool:
        """Whether *closing* the window can leave the application recoverable.

        Requires a tray icon as well as both preferences: hiding the window with no icon, no
        taskbar entry and no menu leaves the app looking like it quit while its downloads keep
        running. Losing the window is strictly worse than losing close-to-tray behaviour, so when
        there is no tray the window closes normally instead.
        """
        cfg = self._manager.general_config
        return bool(cfg.enable_system_tray and cfg.close_to_tray) and self._has_tray_icon()

    def closeEvent(self, event):
        timer = getattr(self, "_tor_availability_timer", None)
        if timer is not None:
            timer.stop()
        cfg = self._manager.general_config
        if not getattr(self, "_force_exit", False) and self._can_hide_to_tray():
            event.ignore()
            self.hide()
            if self._tray_icon and self._tray_icon.isVisible() and not getattr(self, "_close_to_tray_notified", False):
                self._close_to_tray_notified = True
                try:
                    self._tray_icon.showMessage(
                        "Running in Background",
                        "My-IDM is running in the system tray and continuing downloads in the background.",
                        QSystemTrayIcon.MessageIcon.Information,
                        3000,
                    )
                except Exception:
                    pass
            self._update_tray_menu_text()
            return

        if getattr(self, "_is_closing", False):
            event.accept()
            return
        self._is_closing = True

        # Release the OS hotkey and the clipboard subscription on the way out. A hotkey left
        # registered outlives the process: Windows keeps the chord claimed and the next launch
        # cannot take it, with no way for the user to tell why.
        self._release_capture()

        # Close the statistics popup explicitly. It is a child window with its own 1 Hz
        # QTimer, and shutting the manager down underneath it would let that timer fire
        # against a stopped model for the remainder of the teardown.
        stats_dlg = getattr(self, "_stats_dialog", None)
        if stats_dlg is not None:
            try:
                stats_dlg.close()
            except RuntimeError:
                pass
            self._stats_dialog = None

        try:
            from my_idm.notifications import unregister_notification_handler
            unregister_notification_handler(self.show_tray_notification)
        except Exception:
            pass

        # Stop UI timer immediately so no further GUI updates fire
        if hasattr(self, "_details_timer"):
            self._details_timer.stop()

        # Save UI state before hiding so geometry is accurate
        try:
            self._save_ui_state_to_db()
        except Exception as exc:
            log.warning("Failed saving UI state: %s", exc)

        exit_splash = None
        if self._show_exit_splash:
            try:
                from my_idm.splash import IDMExitSplashScreen
                exit_splash = IDMExitSplashScreen()
                # Center exit splash over the closing main window
                splash_x = self.x() + (self.width() - exit_splash.width()) // 2
                splash_y = self.y() + (self.height() - exit_splash.height()) // 2
                exit_splash.move(max(0, splash_x), max(0, splash_y))
                exit_splash.show()
                exit_splash.set_message("Closing My-IDM...", 10)
            except Exception as exc:
                log.warning("Could not show exit splash: %s", exc)
                exit_splash = None

        # Instantly hide main window
        self.hide()

        app = QApplication.instance()

        # Stop the manager BEFORE flushing pending events. processEvents() dispatches queued
        # signals, and a worker thread (the Tor availability probe, the torrent monitor) can
        # still be mid-call and about to emit into this window - re-entering a window that
        # is half torn down is what crashed the app on quit.
        try:
            if exit_splash:
                self._manager.stop(status_cb=exit_splash.set_message)
            else:
                self._manager.stop()
        except Exception as exc:
            log.warning("Error stopping manager during close: %s", exc)

        # Flush all pending window manager messages so main window vanishes immediately
        # and exit splash screen appears on screen with zero delay
        if app:
            app.processEvents()

        try:
            if exit_splash:
                exit_splash.set_message("Goodbye!", 100)
                exit_splash.close()
                if app:
                    app.processEvents()
        finally:
            super().closeEvent(event)
