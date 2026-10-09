"""Bottom details panel for inspecting downloads (Overview, Files, Peers, Trackers, Segments)."""

from __future__ import annotations

import html
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import humanize
from datetime import datetime, timedelta, timezone
from PySide6.QtCore import Qt, Signal, QTimer, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QTextCursor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyleOption,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("my_idm.details_panel")

from my_idm.database import DEFAULT_QUEUE_ID, DownloadEntry
from my_idm.download_model import _format_eta, _format_speed, _format_time
from my_idm.external_tools import embedded_browser_supported, find_chrome_hwnd, launch_animepahe_gui
from my_idm.manager import DownloadManager
from my_idm.styles import Colors, themed, themed_widget
from my_idm.utils import send_to_trash, to_int, unlock_path
from my_idm import fonts


_TORRENT_PRIORITY_MAP = {
    7: "Max (100%)",
    6: "High (75%)",
    4: "Medium (50%)",
    1: "Low (25%)",
    0: "Don't Download",
}

_PRIORITY_TO_VAL = {
    "Max (100%)": 7,
    "High (75%)": 6,
    "Medium (50%)": 4,
    "Low (25%)": 1,
    "Don't Download": 0,
    # Compatibility aliases
    "Max": 7,
    "High": 6,
    "Normal": 4,
    "Medium": 4,
    "Low": 1,
}


def _priority_to_label(prio: int) -> str:
    if prio >= 7:
        return "Max (100%)"
    if prio >= 6:
        return "High (75%)"
    if prio >= 3:
        return "Medium (50%)"
    if prio >= 1:
        return "Low (25%)"
    return "Don't Download"


def _to_str(val: Any) -> str:
    """Safely convert any value (including bytes from libtorrent) to a unicode string."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


class FilesTreeWidget(QTreeWidget):
    """QTreeWidget displaying files and folders hierarchy with table compatibility methods."""

    def rowCount(self) -> int:
        return self.topLevelItemCount()

    def setRowCount(self, count: int):
        if count == 0:
            self.clear()

    def item(self, row: int, col: int):
        if 0 <= row < self.topLevelItemCount():
            top = self.topLevelItem(row)

            class _ItemCompat:
                def __init__(self, it: QTreeWidgetItem, col_idx: int):
                    self._it = it
                    self._col_idx = col_idx

                def text(self) -> str:
                    target_col = 0 if self._col_idx in (0, 2) else self._col_idx
                    t = self._it.text(target_col)
                    for p in ("📁 ", "📄 "):
                        if t.startswith(p):
                            return t[len(p):]
                    return t

            return _ItemCompat(top, col)
        return None

    def cellWidget(self, row: int, col: int):
        if 0 <= row < self.topLevelItemCount():
            top = self.topLevelItem(row)
            if col == 5:
                return self.itemWidget(top, 3)
            if col == 0:
                class _CheckCompat(QCheckBox):
                    def __init__(self, it: QTreeWidgetItem):
                        super().__init__()
                        self._it = it

                    def isChecked(self) -> bool:
                        return self._it.checkState(0) == Qt.CheckState.Checked

                    def setChecked(self, val: bool):
                        self._it.setCheckState(0, Qt.CheckState.Checked if val else Qt.CheckState.Unchecked)

                    def isEnabled(self) -> bool:
                        return bool(self._it.flags() & Qt.ItemFlag.ItemIsUserCheckable)

                chk = _CheckCompat(top)

                class _ContainerCompat(QWidget):
                    def __init__(self, c: QCheckBox):
                        super().__init__()
                        self._c = c

                    def findChild(self, cls, *args, **kwargs):
                        return self._c

                return _ContainerCompat(chk)
            return self.itemWidget(top, col)
        return None


class SideTabBar(QWidget):
    """Vertical tab set on the left side of the panel for switching between Details & Console."""

    currentChanged = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self._current_index = 0
        self._tabs: list[QPushButton] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)

        self._btn_details = self._create_tab_button("📋 Details", 0)
        self._btn_console = self._create_tab_button("📄 Console", 1)

        layout.addWidget(self._btn_details)
        layout.addWidget(self._btn_console)
        layout.addStretch(1)

        self.setFixedWidth(112)
        # `themed`, not an f-string: a QSS rule body is `SideTabBar { ... }` and in an
        # f-string those braces are replacement fields. Storing the literal `Colors.*` names
        # also lets `apply_theme` re-resolve this sheet on a theme switch - an f-string is
        # resolved once, at construction, which left the sidebar white after switching from
        # light back to dark.
        themed_widget(self, """
            SideTabBar {
                background-color: Colors.BG_DARK;
                border-right: 1px solid Colors.BORDER;
            }
        """)

        self.setCurrentIndex(0)

    def paintEvent(self, event):
        opt = QStyleOption()
        opt.initFrom(self)
        p = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, opt, p, self)
        super().paintEvent(event)

    def _create_tab_button(self, text: str, index: int) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(34)
        themed_widget(btn, """
            QPushButton {
                background-color: Colors.BG_DARK;
                color: Colors.TEXT_SECONDARY;
                border: none;
                border-left: 3px solid transparent;
                border-radius: 0px;
                padding: 6px 12px;
                font-size: 12px;
                font-weight: 500;
                text-align: left;
            }
            QPushButton:hover {
                background-color: Colors.BG_HOVER;
                color: Colors.TEXT;
                border: none;
                border-left: 3px solid transparent;
                border-radius: 0px;
            }
            QPushButton:checked {
                background-color: Colors.BG_MID;
                color: Colors.ACCENT;
                border: none;
                border-left: 3px solid Colors.ACCENT;
                border-radius: 0px;
                font-weight: bold;
            }
            QPushButton:pressed {
                background-color: Colors.BG_LIGHT;
                border: none;
                border-left: 3px solid Colors.ACCENT;
                border-radius: 0px;
            }
        """)
        btn.clicked.connect(lambda: self.setCurrentIndex(index))
        self._btn_group.addButton(btn, index)
        self._tabs.append(btn)
        return btn

    def count(self) -> int:
        return len(self._tabs)

    def currentIndex(self) -> int:
        return self._current_index

    def setCurrentIndex(self, index: int):
        if 0 <= index < len(self._tabs):
            self._tabs[index].setChecked(True)
            if self._current_index != index:
                self._current_index = index
                self.currentChanged.emit(index)

    def tabText(self, index: int) -> str:
        if 0 <= index < len(self._tabs):
            return self._tabs[index].text()
        return ""

    def setTabText(self, index: int, text: str):
        if 0 <= index < len(self._tabs):
            self._tabs[index].setText(text)


class SessionTabList(QWidget):
    """Vertical, scrollable list of scraper log sessions.

    One entry per run, newest first, so the live session is always visible
    without scrolling. Deliberately a plain button list inside a scroll area
    rather than a QTabWidget: the log body stays a QPlainTextEdit, which keeps
    native multi-line text selection and Ctrl+C copying.
    """

    currentKeyChanged = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)

        self._keys: list[str] = []
        self._labels: list[str] = []
        self._buttons: list[QPushButton] = []
        self._current_key: str = ""
        self._following_live = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._holder = QWidget()
        self._holder.setObjectName("session_tab_holder")
        self._list_layout = QVBoxLayout(self._holder)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(1)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._holder)
        outer.addWidget(self._scroll)

        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)

        self.setFixedWidth(118)
        themed_widget(self, """
            SessionTabList {
                background-color: Colors.BG_DARK;
                border-right: 1px solid Colors.BORDER;
            }
        """)

    # -- population ---------------------------------------------------------

    @staticmethod
    def _format_label(title: str) -> str:
        """Compact, sortable label for a session banner timestamp."""
        try:
            stamp = datetime.fromisoformat(title.strip())
        except (TypeError, ValueError):
            return title.strip() or "Session"
        if stamp.year == datetime.now().year:
            return stamp.strftime("%d %b %H:%M")
        return stamp.strftime("%d %b %y %H:%M")

    def set_sessions(self, entries: list[tuple[str, str]]) -> None:
        """Replace the list. *entries* is ``[(key, banner_title), ...]``, oldest first.

        The newest session is prepended so it sits at the top of the list.
        """
        ordered = list(reversed(entries))
        self._keys = [k for k, _t in ordered]
        self._labels = [self._format_label(t) for _k, t in ordered]
        self._rebuild_buttons()
        if self._keys:
            if self._current_key in self._keys:
                self._select_key(self._current_key, notify=False)
            else:
                self._select_key(self._keys[0], notify=False)

    def _clear_buttons(self) -> None:
        while self._list_layout.count() > 1:  # keep the trailing stretch
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                self._btn_group.removeButton(widget)
                widget.deleteLater()
        self._buttons.clear()

    def _rebuild_buttons(self) -> None:
        self._clear_buttons()
        for key, label in zip(self._keys, self._labels):
            btn = QPushButton(label, self._holder)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(30)
            btn.setToolTip(key.split("#", 1)[0])
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {Colors.TEXT_SECONDARY};
                    border: none;
                    border-left: 3px solid transparent;
                    border-radius: 0px;
                    padding: 4px 8px;
                    font-size: 11px;
                    text-align: left;
                }}
                QPushButton:hover {{
                    background-color: {Colors.BG_HOVER};
                    color: {Colors.TEXT};
                }}
                QPushButton:checked {{
                    background-color: {Colors.BG_MID};
                    color: {Colors.ACCENT};
                    border-left: 3px solid {Colors.ACCENT};
                    font-weight: bold;
                }}
            """)
            btn.clicked.connect(lambda _c=False, k=key: self._select_key(k))
            self._btn_group.addButton(btn)
            self._list_layout.insertWidget(self._list_layout.count() - 1, btn)
            self._buttons.append(btn)

    # -- selection ----------------------------------------------------------

    def _select_key(self, key: str, notify: bool = True) -> None:
        if key not in self._keys:
            return
        changed = self._current_key != key
        self._current_key = key
        index = self._keys.index(key)
        for i, btn in enumerate(self._buttons):
            btn.setChecked(i == index)
        if changed and notify:
            self.currentKeyChanged.emit(key)

    def current_key(self) -> str:
        return self._current_key

    def set_current_key(self, key: str) -> None:
        self._select_key(key)

    def count(self) -> int:
        return len(self._keys)

    def tabText(self, index: int) -> str:
        return self._labels[index] if 0 <= index < len(self._labels) else ""

    def keyAt(self, index: int) -> str:
        return self._keys[index] if 0 <= index < len(self._keys) else ""

    def is_following_live(self) -> bool:
        """True when the newest session is selected (i.e. the user is tailing)."""
        return bool(self._keys) and self._current_key == self._keys[0]


class EmbeddedBrowserContainer(QWidget):
    """Container widget that embeds an external browser window via Win32 SetParent."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._chrome_hwnd: Optional[int] = None
        self._original_style: Optional[int] = None
        if os.environ.get("CI") != "true":
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setStyleSheet(
            f"background-color: {Colors.BG_DARK}; border: 1px solid {Colors.BORDER}; border-radius: 4px;"
        )

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._placeholder_lbl = QLabel(
            "🌐 Waiting for AnimePahe browser session...\n\n"
            "undetected-chromedriver will automatically dock here when Cloudflare resolution triggers.",
            self,
        )
        self._placeholder_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder_lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: 12px; font-weight: 500;")
        self._layout.addWidget(self._placeholder_lbl)

    def hwnd(self) -> int:
        if sys.platform != "win32":
            return 0
        if os.environ.get("CI") == "true":
            wid = self.internalWinId()
            return int(wid) if wid else 0x1234
        try:
            return int(self.winId())
        except Exception:
            return 0

    @property
    def chrome_hwnd(self) -> Optional[int]:
        return self._chrome_hwnd

    def is_attached(self) -> bool:
        return self._chrome_hwnd is not None

    def attach_window(self, hwnd: int) -> bool:
        if not hwnd:
            return False

        if sys.platform == "win32":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if user32.IsWindow(hwnd):
                    # Prevent double-docking: check if already a child of our container
                    parent = user32.GetParent(ctypes.c_void_p(hwnd))
                    if parent == self.hwnd():
                        # Already attached, just ensure it's visible and positioned
                        self._chrome_hwnd = hwnd
                        self._placeholder_lbl.setVisible(False)
                        w = max(self.width(), 400)
                        h = max(self.height(), 300)
                        user32.MoveWindow(ctypes.c_void_p(hwnd), 0, 0, w, h, True)
                        user32.ShowWindow(ctypes.c_void_p(hwnd), 5)  # SW_SHOW
                        logger.info("Browser HWND %s already attached to container %s", hwnd, self.hwnd())
                        return True
            except Exception:
                pass

        self._chrome_hwnd = hwnd
        self._placeholder_lbl.setVisible(False)

        if sys.platform != "win32":
            return True

        try:
            import ctypes
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return False

            GWL_STYLE = -16
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            self._original_style = style
            # Strip WS_CAPTION, WS_THICKFRAME, WS_POPUP
            style &= ~(0x00C00000 | 0x00040000 | 0x80000000)
            style |= 0x40000000  # WS_CHILD

            if hasattr(user32, "SetWindowLongPtrW"):
                user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE, ctypes.c_ssize_t(style))
            else:
                user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE, ctypes.c_long(style))

            user32.SetParent(ctypes.c_void_p(hwnd), ctypes.c_void_p(self.hwnd()))
            w = max(self.width(), 400)
            h = max(self.height(), 300)
            user32.MoveWindow(ctypes.c_void_p(hwnd), 0, 0, w, h, True)
            user32.ShowWindow(ctypes.c_void_p(hwnd), 5)  # SW_SHOW
            logger.info("Successfully attached browser HWND %s into container %s", hwnd, self.hwnd())
            return True
        except Exception as exc:
            logger.warning("Failed to attach browser window %s: %s", hwnd, exc)
            return False

    def detach_window(self):
        if sys.platform == "win32" and self._chrome_hwnd:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if user32.IsWindow(self._chrome_hwnd):
                    user32.SetParent(ctypes.c_void_p(self._chrome_hwnd), None)
                    if self._original_style is not None:
                        if hasattr(user32, "SetWindowLongPtrW"):
                            user32.SetWindowLongPtrW(
                                ctypes.c_void_p(self._chrome_hwnd), -16, ctypes.c_ssize_t(self._original_style)
                            )
                        else:
                            user32.SetWindowLongW(
                                ctypes.c_void_p(self._chrome_hwnd), -16, ctypes.c_long(self._original_style)
                            )
                    user32.ShowWindow(ctypes.c_void_p(self._chrome_hwnd), 0)  # SW_HIDE
            except Exception as exc:
                logger.debug("Error detaching browser window: %s", exc)
        self._chrome_hwnd = None
        self._original_style = None
        self._placeholder_lbl.setVisible(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if sys.platform == "win32" and self._chrome_hwnd:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if user32.IsWindow(self._chrome_hwnd):
                    user32.MoveWindow(ctypes.c_void_p(self._chrome_hwnd), 0, 0, self.width(), self.height(), True)
            except Exception:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        if sys.platform == "win32" and self._chrome_hwnd:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if user32.IsWindow(self._chrome_hwnd):
                    user32.ShowWindow(ctypes.c_void_p(self._chrome_hwnd), 5)  # SW_SHOW
                    user32.MoveWindow(ctypes.c_void_p(self._chrome_hwnd), 0, 0, self.width(), self.height(), True)
            except Exception:
                pass

    def hideEvent(self, event):
        super().hideEvent(event)
        if sys.platform == "win32" and self._chrome_hwnd:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                if user32.IsWindow(self._chrome_hwnd):
                    user32.ShowWindow(ctypes.c_void_p(self._chrome_hwnd), 0)  # SW_HIDE
            except Exception:
                pass


class DetailsPanel(QWidget):
    """Collapsible and tabbed bottom panel showing details for the selected download and background consoles."""

    close_requested = Signal()
    mode_changed = Signal(str)
    browser_tab_requested = Signal()

    def __init__(self, manager: DownloadManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self._manager = manager
        self._download_id: Optional[str] = None
        self._current_entry: Optional[DownloadEntry] = None
        self._files_hash: Optional[tuple] = None
        self._tree_updating: bool = False
        self._file_item_map: dict[int, QTreeWidgetItem] = {}
        self._folder_items: list[QTreeWidgetItem] = []

        self._raw_log_lines: list[str] = []
        self._log_offset: int = 0
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(250)
        self._log_timer.timeout.connect(self._poll_console_log)

        self._queue_row_widgets: dict[str, dict[str, Any]] = {}

        self._is_browser_floating: bool = False
        self._browser_monitor_timer = QTimer(self)
        self._browser_monitor_timer.setInterval(300)
        self._browser_monitor_timer.timeout.connect(self._on_browser_monitor_tick)

        self._setup_ui()
        self._manager.queues_changed.connect(self._update_queues)
        self._manager.animepahe_status_changed.connect(self.on_animepahe_status_changed)
        if self._manager.is_animepahe_running():
            self._browser_monitor_timer.start()
        self._update_queues()

    def paintEvent(self, event):
        opt = QStyleOption()
        opt.initFrom(self)
        p = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, opt, p, self)
        super().paintEvent(event)

    @property
    def current_download_id(self) -> Optional[str]:
        return self._download_id

    # -- UI Setup -------------------------------------------------------------

    def _setup_ui(self):
        themed_widget(self, """
            DetailsPanel {
                background-color: Colors.BG_DARK;
                border-top: 1px solid Colors.BORDER;
            }
        """)
        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # 1. Left side tab set for Details & Console
        self._side_tabs = SideTabBar(self)
        self._side_tabs.currentChanged.connect(self._on_mode_tab_changed)
        outer_layout.addWidget(self._side_tabs)

        # 2. Right container with header and stacked pages
        right_container = QWidget(self)
        right_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right_container.setAutoFillBackground(True)
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(8, 4, 8, 8)
        right_layout.setSpacing(6)

        # Header Bar
        header_widget = QWidget(right_container)
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(4, 2, 4, 2)
        header_layout.setSpacing(8)

        self._lbl_icon = QLabel("📊", header_widget)
        self._lbl_icon.setStyleSheet("font-size: 16px;")
        header_layout.addWidget(self._lbl_icon)

        self._lbl_title = QLabel("Select a download to view details", header_widget)
        font = self._lbl_title.font()
        font.setBold(True)
        font.setPointSize(11)
        self._lbl_title.setFont(font)
        self._lbl_title.setStyleSheet(f"color: {Colors.TEXT};")
        header_layout.addWidget(self._lbl_title, stretch=1)

        self._lbl_badge = QLabel("", header_widget)
        self._lbl_badge.setStyleSheet(
            f"background-color: {Colors.BG_LIGHT}; color: {Colors.ACCENT}; "
            f"padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;"
        )
        self._lbl_badge.setVisible(False)
        header_layout.addWidget(self._lbl_badge)

        # Status label for temporary messages (e.g., "Copied to clipboard")
        self._status_label = QLabel("", header_widget)
        self._status_label.setStyleSheet(f"color: {Colors.ACCENT}; font-size: 11px;")
        self._status_label.setVisible(False)
        header_layout.addWidget(self._status_label)

        self._btn_open_folder = QPushButton("📁 Open Folder", header_widget)
        self._btn_open_folder.setToolTip("Open containing directory in File Explorer")
        self._btn_open_folder.clicked.connect(self._on_open_folder_clicked)
        self._btn_open_folder.setVisible(False)
        header_layout.addWidget(self._btn_open_folder)

        self._btn_close = QPushButton("✕", header_widget)
        self._btn_close.setObjectName("detailsCloseBtn")
        self._btn_close.setToolTip("Hide bottom panel (F4)")
        self._btn_close.setFixedSize(26, 26)
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close.clicked.connect(self.close_requested.emit)
        header_layout.addWidget(self._btn_close)

        right_layout.addWidget(header_widget)

        # Mode Stack (Details vs Console)
        self._mode_stack = QStackedWidget(right_container)

        # Page 0: Details sub-tabs
        self._tabs = QTabWidget(self)

        # 1. Overview Tab
        self._tab_overview = self._create_overview_tab()
        self._tabs.addTab(self._tab_overview, "📋 Overview")

        # 2. Files Tab
        self._tab_files = self._create_files_tab()
        self._tabs.addTab(self._tab_files, "📁 Files")

        # 3. Peers & Swarm Tab
        self._tab_peers = self._create_peers_tab()
        self._tabs.addTab(self._tab_peers, "👥 Peers && Swarm")

        # 4. Trackers Tab
        self._tab_trackers = self._create_trackers_tab()
        self._tabs.addTab(self._tab_trackers, "📡 Trackers")

        # 5. Segments Tab
        self._tab_segments = self._create_segments_tab()
        self._tabs.addTab(self._tab_segments, "🧩 Segments")

        # 6. Queues Tab
        self._tab_queues = self._create_queues_tab()
        self._tabs.addTab(self._tab_queues, "🗂️ Queues")

        self._tabs.currentChanged.connect(self._on_details_tab_changed)
        self._mode_stack.addWidget(self._tabs)

        # Page 1: Console View
        self._console_widget = self._create_console_view()
        self._tab_console = self._console_widget
        self._mode_stack.addWidget(self._console_widget)

        right_layout.addWidget(self._mode_stack, stretch=1)
        outer_layout.addWidget(right_container, stretch=1)

    def _create_overview_tab(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # Info grid container
        grid_widget = QWidget(container)
        grid_layout = QHBoxLayout(grid_widget)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(24)

        # Left column
        left_col = QVBoxLayout()
        left_col.setSpacing(6)

        self._ov_status, _ = self._create_info_row(left_col, "Status:")
        self._ov_size, _ = self._create_info_row(left_col, "Size:")
        self._ov_downloaded, _ = self._create_info_row(left_col, "Downloaded:")
        self._ov_seeded, _ = self._create_info_row(left_col, "Total Seeded / Uploaded:")
        self._ov_speed, _ = self._create_info_row(left_col, "Speed:")
        self._ov_eta, _ = self._create_info_row(left_col, "ETA:")
        self._ov_added, _ = self._create_info_row(left_col, "Added:")
        self._ov_completed, _ = self._create_info_row(left_col, "Completed:")
        left_col.addStretch()
        grid_layout.addLayout(left_col, stretch=1)

        # Right column
        right_col = QVBoxLayout()
        right_col.setSpacing(6)

        self._ov_filename, self._ov_filename_copy_btn = self._create_info_row(right_col, "File / Folder Name:", copyable=True)
        self._ov_anime_title, self._ov_anime_title_copy_btn = self._create_info_row(right_col, "Show Title:", copyable=True)
        self._ov_type, _ = self._create_info_row(right_col, "Transfer Type:")
        self._ov_swarm, _ = self._create_info_row(right_col, "Swarm / Parts:")
        self._ov_save_path, self._ov_save_path_copy_btn = self._create_info_row(right_col, "Save Directory:", copyable=True)
        self._ov_hash, self._ov_hash_copy_btn = self._create_info_row(right_col, "Content Hash / Infohash:", copyable=True)
        self._ov_security, _ = self._create_info_row(right_col, "Malware Scan:")
        self._ov_url, self._ov_url_copy_btn = self._create_info_row(right_col, "Source URL / Magnet:", copyable=True)
        self._ov_anime_url, self._ov_anime_url_copy_btn = self._create_info_row(right_col, "Show URL:", copyable=True)
        self._ov_referrer, self._ov_referrer_copy_btn = self._create_info_row(right_col, "Referrer:", copyable=True)
        right_col.addStretch()
        grid_layout.addLayout(right_col, stretch=1)

        layout.addWidget(grid_widget)
        scroll.setWidget(container)
        return scroll

    def _create_info_row(self, parent_layout: QVBoxLayout, label_text: str, copyable: bool = False) -> tuple[QLabel, QPushButton]:
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label_text)
        lbl.setFixedWidth(160)
        lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-weight: 500;")
        val = QLabel("—")
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        val.setWordWrap(False)
        val.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        row.addWidget(lbl)
        row.addWidget(val)  # No stretch - let it size to content
        
        copy_btn = None
        if copyable:
            # Copy button
            copy_btn = QPushButton()
            copy_btn.setIcon(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
            copy_btn.setFixedSize(18, 18)
            copy_btn.setIconSize(QSize(12, 12))
            copy_btn.setToolTip(f"Copy {label_text.strip(':')}")
            copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    border: none;
                    padding: 0px;
                }}
                QPushButton:hover {{
                    background-color: {Colors.BG_HOVER};
                    border-radius: 3px;
                }}
            """)
            copy_btn.clicked.connect(lambda _, v=val: self._copy_to_clipboard(v.text()))
            copy_btn.setVisible(False)  # Hidden by default
            row.addWidget(copy_btn)
        
        row.addStretch()  # Push everything left
        parent_layout.addLayout(row)
        return val, copy_btn

    def _create_files_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._tree_files = FilesTreeWidget(container)
        self._tree_files.setHeaderLabels([
            "Name", "Size", "Progress", "Priority", "Status"
        ])
        header = self._tree_files.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._tree_files.setColumnWidth(2, 140)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._tree_files.setColumnWidth(3, 130)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._tree_files.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self._tree_files.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self._tree_files.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)

        self._tree_files.setStyleSheet(f"""
            QTreeWidget {{
                background-color: {Colors.BG_MID};
                color: {Colors.TEXT};
                border: 1px solid {Colors.BORDER};
                font-size: 12px;
            }}
            QTreeWidget::item {{
                padding: 3px 0px;
                border-bottom: 1px solid {Colors.BG_HOVER};
            }}
            QHeaderView::section {{
                background-color: {Colors.BG_DARK};
                color: {Colors.TEXT_SECONDARY};
                padding: 4px 8px;
                font-weight: bold;
                font-size: 11px;
                border: 1px solid {Colors.BORDER};
            }}
        """)

        self._tree_files.itemChanged.connect(self._on_tree_item_changed)
        self._tree_files.itemDoubleClicked.connect(self._on_tree_item_double_clicked)
        self._tree_files.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree_files.customContextMenuRequested.connect(self._show_files_context_menu)
        self._table_files = self._tree_files

        layout.addWidget(self._tree_files)
        return container

    def _create_peers_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._lbl_peers_status = QLabel("", container)
        self._lbl_peers_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(self._lbl_peers_status)

        self._table_peers = QTableWidget(0, 6, container)
        self._table_peers.setHorizontalHeaderLabels([
            "IP Address : Port", "Client", "Progress", "Down Speed", "Up Speed", "Flags"
        ])
        header = self._table_peers.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table_peers.verticalHeader().setVisible(False)
        self._table_peers.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_peers.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self._table_peers)
        return container

    def _create_trackers_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._lbl_trackers_status = QLabel("", container)
        self._lbl_trackers_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(self._lbl_trackers_status)

        self._table_trackers = QTableWidget(0, 4, container)
        self._table_trackers.setHorizontalHeaderLabels([
            "Tier", "Tracker URL", "Status", "Send Stats"
        ])
        header = self._table_trackers.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table_trackers.verticalHeader().setVisible(False)
        self._table_trackers.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_trackers.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self._table_trackers)
        return container

    def _create_segments_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        self._lbl_segments_status = QLabel("", container)
        self._lbl_segments_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(self._lbl_segments_status)

        self._table_segments = QTableWidget(0, 5, container)
        self._table_segments.setHorizontalHeaderLabels([
            "Segment #", "Byte Range", "Downloaded", "Progress", "Status"
        ])
        header = self._table_segments.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table_segments.setColumnWidth(3, 140)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table_segments.verticalHeader().setVisible(False)
        self._table_segments.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_segments.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self._table_segments)
        return container

    def _create_queues_tab(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._lbl_queues_status = QLabel(
            "Named download queues, concurrency budgets, and limits. Pause or resume each queue individually below.",
            container,
        )
        self._lbl_queues_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(self._lbl_queues_status)

        self._table_queues = QTableWidget(0, 8, container)
        self._table_queues.setHorizontalHeaderLabels([
            "Queue", "Status", "Downloads", "Speed", "Max at Once", "Download Limit", "Upload Limit", "Actions"
        ])
        header = self._table_queues.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._table_queues.setColumnWidth(4, 100)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self._table_queues.setColumnWidth(5, 125)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self._table_queues.setColumnWidth(6, 125)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)

        self._table_queues.verticalHeader().setVisible(False)
        self._table_queues.verticalHeader().setDefaultSectionSize(32)
        self._table_queues.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table_queues.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table_queues.setShowGrid(True)
        self._table_queues.itemDoubleClicked.connect(self._on_queue_row_double_clicked)

        layout.addWidget(self._table_queues)
        return container

    def _show_status_message(self, message: str):
        if hasattr(self, "_status_label"):
            self._status_label.setText(message)
            self._status_label.setVisible(True)
            QTimer.singleShot(2500, lambda: self._status_label.setVisible(False))

    def _update_queues_header(self):
        if self.current_mode() == "console":
            return
        self._lbl_icon.setText("🗂️")
        self._lbl_title.setText("Download Queues & Concurrency")
        queues = self._manager.get_queues()
        self._lbl_badge.setText(f"{len(queues)} Queues")
        self._lbl_badge.setStyleSheet(
            f"background-color: {Colors.BG_LIGHT}; color: {Colors.ACCENT}; "
            f"padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;"
        )
        self._lbl_badge.setVisible(True)
        self._btn_open_folder.setVisible(False)

    def _get_all_download_entries(self) -> list[DownloadEntry]:
        win = self.window()
        if win is not None and hasattr(win, "_model") and hasattr(win._model, "_all_entries"):
            return list(win._model._all_entries)
        if hasattr(self._manager, "_db") and self._manager._db:
            return self._manager._db.get_all_downloads()
        return []

    def _update_queues(self):
        if not hasattr(self, "_table_queues"):
            return
        queues = self._manager.get_queues()
        entries = self._get_all_download_entries()

        # Aggregate per-queue statistics
        stats: dict[str, dict[str, Any]] = {}
        for q in queues:
            stats[q.id] = {
                "active": 0,
                "queued": 0,
                "paused": 0,
                "stopped": 0,
                "completed": 0,
                "error": 0,
                "total": 0,
                "down_speed": 0.0,
                "up_speed": 0.0,
            }

        default_qid = queues[0].id if queues else DEFAULT_QUEUE_ID
        for e in entries:
            qid = e.queue_id or default_qid
            if hasattr(self._manager, "_db") and self._manager._db:
                qid = self._manager._db.resolve_queue_id(qid)
            if qid not in stats:
                qid = default_qid
            st = stats.get(qid)
            if not st:
                continue
            st["total"] += 1
            if e.status in ("downloading", "checking", "fetching_metadata", "stalled", "seeding"):
                st["active"] += 1
                st["down_speed"] += float(getattr(e, "speed", 0.0) or 0.0)
                st["up_speed"] += float(getattr(e, "upload_speed", 0.0) or 0.0)
            elif e.status == "queued":
                st["queued"] += 1
            elif e.status == "paused":
                st["paused"] += 1
            elif e.status == "stopped":
                st["stopped"] += 1
            elif e.status in ("completed", "seeding"):
                st["completed"] += 1
            elif e.status == "error":
                st["error"] += 1

        needs_rebuild = (
            self._table_queues.rowCount() != len(queues)
            or set(self._queue_row_widgets.keys()) != set(q.id for q in queues)
        )

        if needs_rebuild:
            self._table_queues.setRowCount(0)
            self._queue_row_widgets.clear()
            self._table_queues.setRowCount(len(queues))

            for row_idx, q in enumerate(queues):
                # Col 0: Swatch + Name
                holder = QWidget()
                h_lay = QHBoxLayout(holder)
                h_lay.setContentsMargins(6, 2, 6, 2)
                h_lay.setSpacing(8)
                swatch = QLabel()
                swatch.setFixedSize(14, 14)
                color = q.color or Colors.ACCENT
                swatch.setStyleSheet(f"background-color: {color}; border-radius: 3px; border: 1px solid #555;")
                suffix = " (Default)" if q.is_default else ""
                name_lbl = QLabel(f"{q.name}{suffix}")
                name_lbl.setStyleSheet(f"color: {Colors.TEXT}; font-weight: bold;")
                h_lay.addWidget(swatch)
                h_lay.addWidget(name_lbl)
                h_lay.addStretch()
                self._table_queues.setCellWidget(row_idx, 0, holder)

                # Col 1: Status
                item_status = QTableWidgetItem("Idle")
                item_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_queues.setItem(row_idx, 1, item_status)

                # Col 2: Downloads
                item_counts = QTableWidgetItem("0 downloads")
                self._table_queues.setItem(row_idx, 2, item_counts)

                # Col 3: Speed
                item_speed = QTableWidgetItem("—")
                item_speed.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_queues.setItem(row_idx, 3, item_speed)

                # Col 4: Max at Once
                spin_max = QSpinBox()
                spin_max.setRange(0, 99)
                spin_max.setAlignment(Qt.AlignmentFlag.AlignCenter)
                spin_max.setValue(max(0, q.max_concurrent))
                spin_max.setToolTip(
                    f"Concurrency ceiling for '{q.name}'.\n"
                    "0 = follow global limit (no ceiling of its own)."
                )
                spin_max.valueChanged.connect(
                    lambda val, qid=q.id: self._on_queue_max_concurrent_changed(qid, val)
                )
                self._table_queues.setCellWidget(row_idx, 4, spin_max)

                # Col 5: Download Limit
                spin_dl = QSpinBox()
                spin_dl.setRange(0, 10_000_000)
                spin_dl.setSingleStep(64)
                spin_dl.setSuffix(" KB/s")
                spin_dl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                spin_dl.setValue(max(0, int(q.download_limit or 0) // 1024))
                spin_dl.setToolTip(
                    f"Download rate ceiling for '{q.name}' in KB/s.\n"
                    "0 = follow global limit (no ceiling of its own)."
                )
                spin_dl.valueChanged.connect(
                    lambda val, qid=q.id: self._on_queue_dl_limit_changed(qid, val)
                )
                self._table_queues.setCellWidget(row_idx, 5, spin_dl)

                # Col 6: Upload Limit
                spin_up = QSpinBox()
                spin_up.setRange(0, 10_000_000)
                spin_up.setSingleStep(64)
                spin_up.setSuffix(" KB/s")
                spin_up.setAlignment(Qt.AlignmentFlag.AlignCenter)
                spin_up.setValue(max(0, int(q.upload_limit or 0) // 1024))
                spin_up.setToolTip(
                    f"Upload rate ceiling for '{q.name}' in KB/s.\n"
                    "0 = follow global limit (no ceiling of its own)."
                )
                spin_up.valueChanged.connect(
                    lambda val, qid=q.id: self._on_queue_up_limit_changed(qid, val)
                )
                self._table_queues.setCellWidget(row_idx, 6, spin_up)

                # Col 7: Actions
                act_holder = QWidget()
                act_lay = QHBoxLayout(act_holder)
                act_lay.setContentsMargins(4, 2, 4, 2)
                act_lay.setSpacing(6)
                btn_pause = QPushButton("⏸ Pause")
                btn_pause.setToolTip(f"Pause all active and queued downloads in '{q.name}'")
                btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
                btn_pause.clicked.connect(lambda _=False, qid=q.id: self._on_pause_queue_clicked(qid))

                btn_resume = QPushButton("▶ Resume")
                btn_resume.setToolTip(f"Resume all paused and stopped downloads in '{q.name}'")
                btn_resume.setCursor(Qt.CursorShape.PointingHandCursor)
                btn_resume.clicked.connect(lambda _=False, qid=q.id: self._on_resume_queue_clicked(qid))

                act_lay.addWidget(btn_pause)
                act_lay.addWidget(btn_resume)
                self._table_queues.setCellWidget(row_idx, 7, act_holder)

                self._queue_row_widgets[q.id] = {
                    "row": row_idx,
                    "item_status": item_status,
                    "item_counts": item_counts,
                    "item_speed": item_speed,
                    "spin_max": spin_max,
                    "spin_dl": spin_dl,
                    "spin_up": spin_up,
                    "btn_pause": btn_pause,
                    "btn_resume": btn_resume,
                }

        # Update values for each row without recreating widgets
        for q in queues:
            widgets = self._queue_row_widgets.get(q.id)
            if not widgets:
                continue
            st = stats.get(q.id, {})

            active = st.get("active", 0)
            queued = st.get("queued", 0)
            paused = st.get("paused", 0)
            stopped = st.get("stopped", 0)
            completed = st.get("completed", 0)
            error = st.get("error", 0)
            total = st.get("total", 0)
            down_spd = st.get("down_speed", 0.0)
            up_spd = st.get("up_speed", 0.0)

            # Status column
            if active > 0:
                status_text = f"Running ({active})"
                widgets["item_status"].setForeground(QColor(Colors.ACCENT))
            elif queued > 0:
                status_text = f"Queued ({queued})"
                widgets["item_status"].setForeground(QColor(Colors.TEXT))
            elif paused > 0:
                status_text = f"Paused ({paused})"
                widgets["item_status"].setForeground(QColor(Colors.ORANGE))
            elif total > 0 and completed == total:
                status_text = "Completed"
                widgets["item_status"].setForeground(QColor(Colors.GREEN))
            elif error > 0:
                status_text = f"Error ({error})"
                widgets["item_status"].setForeground(QColor(Colors.RED))
            else:
                status_text = "Idle"
                widgets["item_status"].setForeground(QColor(Colors.TEXT_MUTED))
            widgets["item_status"].setText(status_text)

            # Downloads count breakdown
            parts = []
            if active > 0:
                parts.append(f"{active} active")
            if queued > 0:
                parts.append(f"{queued} queued")
            if paused > 0:
                parts.append(f"{paused} paused")
            if error > 0:
                parts.append(f"{error} error")
            if not parts:
                counts_str = f"{total} total" if total > 0 else "Empty"
            else:
                counts_str = ", ".join(parts) + f" ({total} total)"
            widgets["item_counts"].setText(counts_str)

            # Speed
            if down_spd > 0 or up_spd > 0:
                speed_parts = []
                if down_spd > 0:
                    speed_parts.append(f"↓ {_format_speed(down_spd)}")
                if up_spd > 0:
                    speed_parts.append(f"↑ {_format_speed(up_spd)}")
                widgets["item_speed"].setText("  ".join(speed_parts))
            else:
                widgets["item_speed"].setText("—")

            # Update spinboxes only when not focused
            spin_max = widgets["spin_max"]
            if not spin_max.hasFocus():
                spin_max.blockSignals(True)
                spin_max.setValue(max(0, q.max_concurrent))
                spin_max.blockSignals(False)

            spin_dl = widgets["spin_dl"]
            if not spin_dl.hasFocus():
                spin_dl.blockSignals(True)
                spin_dl.setValue(max(0, int(q.download_limit or 0) // 1024))
                spin_dl.blockSignals(False)

            spin_up = widgets["spin_up"]
            if not spin_up.hasFocus():
                spin_up.blockSignals(True)
                spin_up.setValue(max(0, int(q.upload_limit or 0) // 1024))
                spin_up.blockSignals(False)

            # Action button states
            widgets["btn_pause"].setEnabled(active > 0 or queued > 0)
            widgets["btn_resume"].setEnabled(paused > 0 or stopped > 0)

    def _on_queue_max_concurrent_changed(self, queue_id: str, value: int):
        self._manager.set_queue_max_concurrent(queue_id, value)
        q = self._manager.get_queue(queue_id)
        name = q.name if q else queue_id
        limit_txt = f"{value} concurrent" if value > 0 else "Unlimited (Global)"
        self._show_status_message(f"Updated '{name}' max at once to {limit_txt}")

    def _on_queue_dl_limit_changed(self, queue_id: str, value_kb: int):
        q = self._manager.get_queue(queue_id)
        up_lim = q.upload_limit if q else 0
        self._manager.set_queue_limits(queue_id, value_kb * 1024, up_lim)
        name = q.name if q else queue_id
        limit_txt = f"{value_kb} KB/s" if value_kb > 0 else "Unlimited"
        self._show_status_message(f"Updated '{name}' download limit to {limit_txt}")

    def _on_queue_up_limit_changed(self, queue_id: str, value_kb: int):
        q = self._manager.get_queue(queue_id)
        dl_lim = q.download_limit if q else 0
        self._manager.set_queue_limits(queue_id, dl_lim, value_kb * 1024)
        name = q.name if q else queue_id
        limit_txt = f"{value_kb} KB/s" if value_kb > 0 else "Unlimited"
        self._show_status_message(f"Updated '{name}' upload limit to {limit_txt}")

    def _on_pause_queue_clicked(self, queue_id: str):
        count = self._manager.pause_queue(queue_id)
        q = self._manager.get_queue(queue_id)
        name = q.name if q else queue_id
        self._show_status_message(f"Paused {count} download(s) in queue '{name}'")
        self._update_queues()

    def _on_resume_queue_clicked(self, queue_id: str):
        count = self._manager.resume_queue(queue_id)
        q = self._manager.get_queue(queue_id)
        name = q.name if q else queue_id
        self._show_status_message(f"Resumed {count} download(s) in queue '{name}'")
        self._update_queues()

    def _on_queue_row_double_clicked(self, item: QTableWidgetItem):
        row = item.row()
        for qid, w in self._queue_row_widgets.items():
            if w.get("row") == row:
                self._manager.set_active_queue(qid)
                q = self._manager.get_queue(qid)
                name = q.name if q else qid
                self._show_status_message(f"Filtered view to queue '{name}'")
                break

    # -- Public control -------------------------------------------------------

    def set_download_id(self, download_id: Optional[str]):
        """Set or clear the inspected download ID and refresh views."""
        self._download_id = download_id
        self.refresh()

    def _get_entry(self, download_id: str) -> Optional[DownloadEntry]:
        win = self.window()
        if win is not None and hasattr(win, "_model") and hasattr(win._model, "get_entry_by_id"):
            entry = win._model.get_entry_by_id(download_id)
            if entry:
                return entry
        return self._manager.get_entry(download_id)

    def refresh(self):
        """Update all tabs for the active download and global queues."""
        self._update_queues()
        if not self._download_id:
            self._clear_view()
            return

        entry = self._get_entry(self._download_id)
        if not entry:
            self._clear_view()
            return

        self._current_entry = entry

        # Peers & Trackers tabs are only relevant for BitTorrent
        is_torrent = (entry.download_type == "torrent")
        peers_tab_idx = self._tabs.indexOf(self._tab_peers)
        if peers_tab_idx != -1:
            self._tabs.setTabVisible(peers_tab_idx, is_torrent)
            if not is_torrent and self._tabs.currentIndex() == peers_tab_idx:
                self._tabs.setCurrentIndex(0)
        trackers_tab_idx = self._tabs.indexOf(self._tab_trackers)
        if trackers_tab_idx != -1:
            self._tabs.setTabVisible(trackers_tab_idx, is_torrent)
            if not is_torrent and self._tabs.currentIndex() == trackers_tab_idx:
                self._tabs.setCurrentIndex(0)

        if self.current_mode() == "details":
            if hasattr(self, "_tab_queues") and self._tabs.currentWidget() == self._tab_queues:
                self._update_queues_header()
            else:
                self._update_header(entry)
        self._update_overview(entry)
        self._update_files(entry)
        self._update_peers(entry)
        self._update_trackers(entry)
        self._update_segments(entry)

    # -- Internal update methods ----------------------------------------------

    def _clear_header(self):
        if self.current_mode() == "console":
            self._update_console_header()
            return
        if hasattr(self, "_tab_queues") and self._tabs.currentWidget() == self._tab_queues:
            self._update_queues_header()
            return
        self._lbl_icon.setText("📊")
        self._lbl_title.setText("Select a download to view details")
        self._lbl_badge.setVisible(False)
        self._btn_open_folder.setText("📁 Open Folder")
        self._btn_open_folder.setVisible(False)

    def _clear_view(self):
        if self.current_mode() == "details":
            self._clear_header()

        # Clear overview
        self._ov_status.setText("—")
        self._ov_size.setText("—")
        self._ov_downloaded.setText("—")
        self._ov_seeded.setText("—")
        self._ov_speed.setText("—")
        self._ov_eta.setText("—")
        self._ov_added.setText("—")
        self._ov_completed.setText("—")
        self._ov_type.setText("—")
        self._ov_swarm.setText("—")
        self._ov_filename.setText("—")
        self._ov_anime_title.setText("—")
        self._ov_save_path.setText("—")
        self._ov_hash.setText("—")
        self._ov_security.setText("—")
        self._ov_url.setText("—")
        self._ov_anime_url.setText("—")
        self._ov_referrer.setText("—")

        self._table_files.setRowCount(0)
        self._table_peers.setRowCount(0)
        self._table_trackers.setRowCount(0)
        self._table_segments.setRowCount(0)

        self._lbl_peers_status.setText("")
        self._lbl_trackers_status.setText("")
        self._lbl_segments_status.setText("")

    def _update_header(self, entry: DownloadEntry):
        if self.current_mode() == "console":
            return
        if hasattr(self, "_tab_queues") and self._tabs.currentWidget() == self._tab_queues:
            self._update_queues_header()
            return

        icon = "📦" if entry.download_type == "torrent" else "🌐"
        self._lbl_icon.setText(icon)

        from my_idm.download_model import DownloadTableModel
        name = DownloadTableModel.get_original_name(entry)
        self._lbl_title.setText(name)

        badge_type = "BitTorrent" if entry.download_type == "torrent" else "HTTP / Direct"
        self._lbl_badge.setText(badge_type)
        self._lbl_badge.setStyleSheet(
            f"background-color: {Colors.BG_LIGHT}; color: {Colors.ACCENT}; "
            f"padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;"
        )
        self._lbl_badge.setVisible(True)
        self._btn_open_folder.setText("📁 Open Folder")
        self._btn_open_folder.setVisible(bool(entry.save_path or entry.file_path))

    def _copy_to_clipboard(self, text: str):
        """Copy text to clipboard and show confirmation in status label."""
        if not text or text == "—":
            return
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer
        QApplication.clipboard().setText(text)
        # Show message in header status label
        self._status_label.setText("Copied to clipboard")
        self._status_label.setVisible(True)
        QTimer.singleShot(2000, lambda: self._status_label.setVisible(False))

    def _update_overview(self, entry: DownloadEntry):
        # Status with color
        status_color = Colors.ACCENT
        if entry.status == "completed":
            status_color = Colors.GREEN
        elif entry.status == "seeding":
            status_color = Colors.PURPLE
        elif entry.status == "error":
            status_color = Colors.RED
        elif entry.status == "paused":
            status_color = Colors.ORANGE
        elif entry.status == "threat_detected":
            status_color = Colors.RED

        err = f" ({entry.error_message})" if entry.error_message else ""
        self._ov_status.setText(f"<span style='color: {status_color}; font-weight: bold;'>{entry.status.capitalize()}</span>{err}")

        # Size & progress
        total_size = entry.total_size
        downloaded_size = entry.downloaded_size
        if entry.status in ("completed", "seeding"):
            if total_size > 0:
                downloaded_size = max(downloaded_size, total_size)
            elif downloaded_size > 0:
                total_size = downloaded_size
            pct = 100.0
        else:
            pct = (downloaded_size / total_size * 100) if total_size > 0 else 0.0

        total_str = humanize.naturalsize(total_size, binary=True) if total_size > 0 else "Unknown"
        dl_str = humanize.naturalsize(downloaded_size, binary=True)
        self._ov_size.setText(f"{total_str} ({pct:.1f}%)")
        self._ov_downloaded.setText(f"{dl_str} / {total_str}")

        # Total Seeded / Uploaded
        seeded_bytes = getattr(entry, "uploaded_size", 0) or (entry.metadata.get("total_seeded_bytes", 0) if entry.metadata else 0)
        if entry.download_type == "torrent":
            ratio_str = ""
            if downloaded_size > 0 and seeded_bytes > 0:
                ratio = seeded_bytes / downloaded_size
                ratio_str = f"  (Ratio: {ratio:.2f})"
            self._ov_seeded.setText(f"{humanize.naturalsize(seeded_bytes, binary=True)}{ratio_str}" if seeded_bytes > 0 else "0 B")
        else:
            self._ov_seeded.setText("—")

        # Speeds
        down_speed = _format_speed(entry.speed)
        up_speed = _format_speed(entry.upload_speed)
        if entry.download_type == "torrent":
            self._ov_speed.setText(f"↓ {down_speed}   |   ↑ {up_speed}")
            ts = getattr(entry, "total_seeds", 0) or (entry.metadata.get("total_seeds", 0) if entry.metadata else 0)
            tp = getattr(entry, "total_peers", 0) or (entry.metadata.get("total_peers", 0) if entry.metadata else 0)
            seeds = to_int(entry.seeds)
            peers = to_int(entry.peers)
            ts = to_int(ts)
            tp = to_int(tp)
            s_str = f"{seeds} ({ts})" if ts > seeds else f"{seeds}"
            p_str = f"{peers} ({tp})" if tp > peers else f"{peers}"
            self._ov_swarm.setText(f"{s_str} seeds, {p_str} peers connected")
        else:
            self._ov_speed.setText(f"↓ {down_speed}")
            self._ov_swarm.setText(f"{entry.num_segments} HTTP parallel segments")

        self._ov_eta.setText(_format_eta(entry.eta_seconds))
        self._ov_added.setText(_format_time(entry.added_at))
        self._ov_completed.setText(_format_time(entry.completed_at))

        self._ov_type.setText("BitTorrent Swarm" if entry.download_type == "torrent" else "HTTP / Multi-Segment")
        from my_idm.download_model import DownloadTableModel
        fn = DownloadTableModel.get_actual_name(entry)
        self._ov_filename.setText(fn)
        self._ov_save_path.setText(entry.save_path or "—")

        # Hash / Infohash
        hash_val = entry.torrent_info_hash or entry.content_hash or entry.etag or "—"
        self._ov_hash.setText(hash_val)

        # Security
        meta = entry.metadata
        if meta.get("threat_detected"):
            rep = meta.get("antivirus_report", "Malware detected!")
            self._ov_security.setText(f"<span style='color: {Colors.RED}; font-weight: bold;'>⚠ Threat Detected: {html.escape(rep)}</span>")
        elif meta.get("antivirus_scanned"):
            self._ov_security.setText(f"<span style='color: {Colors.GREEN}; font-weight: bold;'>✔ Clean (Scanned)</span>")
        elif meta.get("antivirus_scan_error"):
            # A scanner that could not run. Deliberately neither green nor grey: the file was not
            # cleared, and saying "Not scanned yet" would imply it is still pending rather than
            # that no scanner exists.
            err = html.escape(str(meta["antivirus_scan_error"]))
            self._ov_security.setText(
                f"<span style='color: {Colors.ORANGE};'>⚠ Not scanned — {err}</span>"
            )
        else:
            self._ov_security.setText(
                f"<span style='color: {Colors.TEXT_SECONDARY};'>Not scanned yet</span>"
            )

        # URL
        url_text = entry.url
        if len(url_text) > 80:
            url_text = url_text[:77] + "..."
        self._ov_url.setText(url_text)

        # Anime Title (from metadata)
        anime_title = ""
        if entry.metadata:
            anime_title = entry.metadata.get("anime_title", "")
        if anime_title:
            if len(anime_title) > 80:
                anime_title = anime_title[:77] + "..."
            self._ov_anime_title.setText(anime_title)
        else:
            self._ov_anime_title.setText("—")

        # Anime URL (from metadata)
        anime_url = ""
        if entry.metadata:
            anime_url = entry.metadata.get("anime_url", "")
        if anime_url:
            if len(anime_url) > 80:
                anime_url = anime_url[:77] + "..."
            self._ov_anime_url.setText(anime_url)
        else:
            self._ov_anime_url.setText("—")

        # Referrer (from metadata or headers)
        referrer = ""
        if entry.metadata:
            headers_dict = entry.metadata.get("headers") if isinstance(entry.metadata.get("headers"), dict) else {}
            referrer = (
                entry.metadata.get("referer", "")
                or entry.metadata.get("referrer", "")
                or headers_dict.get("Referer", "")
                or headers_dict.get("referer", "")
            )
        if referrer:
            if len(referrer) > 80:
                referrer = referrer[:77] + "..."
            self._ov_referrer.setText(referrer)
        else:
            self._ov_referrer.setText("—")

        # Show/hide copy buttons based on whether value is not "—"
        self._ov_filename_copy_btn.setVisible(self._ov_filename.text() != "—")
        self._ov_anime_title_copy_btn.setVisible(self._ov_anime_title.text() != "—")
        self._ov_save_path_copy_btn.setVisible(self._ov_save_path.text() != "—")
        self._ov_hash_copy_btn.setVisible(self._ov_hash.text() != "—")
        self._ov_url_copy_btn.setVisible(self._ov_url.text() != "—")
        self._ov_anime_url_copy_btn.setVisible(self._ov_anime_url.text() != "—")
        self._ov_referrer_copy_btn.setVisible(self._ov_referrer.text() != "—")

    def _update_files(self, entry: DownloadEntry):
        files = self._manager.get_download_files(entry.id, entry=entry)
        if not isinstance(files, list) or not files:
            self._files_hash = None
            self._file_item_map.clear()
            self._folder_items.clear()
            self._tree_files.clear()
            return

        is_torrent = (entry.download_type == "torrent")
        structure_hash = tuple((f.get("index", i), str(f.get("path", ""))) for i, f in enumerate(files))
        if self._files_hash != structure_hash:
            self._files_hash = structure_hash
            self._build_files_tree(files, is_torrent)

        self._update_file_values(files, is_torrent)

    def _build_files_tree(self, files: list[dict], is_torrent: bool):
        self._tree_files.blockSignals(True)
        self._tree_files.clear()
        self._file_item_map.clear()
        self._folder_items.clear()

            # Build folder hierarchy
        root_nodes: dict[str, dict] = {}
        for f in files:
            raw_path = str(f.get("path", "file")).replace("\\", "/").strip("/")
            parts = [p for p in raw_path.split("/") if p]
            if not parts:
                parts = ["file"]
            if len(parts) == 1:
                root_nodes[parts[0]] = {"type": "file", "name": parts[0], "data": f}
            else:
                curr = root_nodes
                for p in parts[:-1]:
                    if p not in curr or curr[p]["type"] != "folder":
                        curr[p] = {"type": "folder", "name": p, "children": {}}
                    curr = curr[p]["children"]
                curr[parts[-1]] = {"type": "file", "name": parts[-1], "data": f}

        progress_style = (
            f"QProgressBar {{ border: 1px solid {Colors.BORDER}; border-radius: 3px; background: {Colors.BG_DARK}; height: 16px; text-align: center; font-size: 10px; color: {Colors.TEXT}; }} "
            f"QProgressBar::chunk {{ background: {Colors.ACCENT}; border-radius: 2px; }}"
        )

        def _create_items(parent_widget_or_item, node_dict: dict):
            for name, node in node_dict.items():
                if node["type"] == "folder":
                    item = QTreeWidgetItem(parent_widget_or_item)
                    item.setText(0, f"📁 {name}")
                    item.setData(0, Qt.ItemDataRole.UserRole, {"is_folder": True, "name": name})
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
                    if is_torrent:
                        item.setCheckState(0, Qt.CheckState.Checked)
                    else:
                        item.setCheckState(0, Qt.CheckState.Checked)
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    self._folder_items.append(item)

                    pb = QProgressBar()
                    pb.setRange(0, 100)
                    pb.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    pb.setStyleSheet(progress_style)
                    self._tree_files.setItemWidget(item, 2, pb)

                    if is_torrent:
                        combo = QComboBox()
                        for p_text in ["Max (100%)", "High (75%)", "Medium (50%)", "Low (25%)", "Don't Download"]:
                            combo.addItem(p_text, _PRIORITY_TO_VAL[p_text])
                        combo.setCurrentText("Medium (50%)")
                        combo.currentIndexChanged.connect(lambda idx, it=item: self._on_folder_priority_changed(it))
                        self._tree_files.setItemWidget(item, 3, combo)
                    else:
                        item.setText(3, "Medium (50%)")

                    _create_items(item, node["children"])
                else:
                    f_data = node["data"]
                    f_idx = f_data.get("index", 0)
                    item = QTreeWidgetItem(parent_widget_or_item)
                    item.setText(0, f"📄 {name}")
                    item.setData(0, Qt.ItemDataRole.UserRole, {"is_folder": False, "file_index": f_idx, "data": f_data})
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)

                    curr_prio = f_data.get("priority", 4)
                    is_dl = (curr_prio > 0)
                    if is_torrent:
                        item.setCheckState(0, Qt.CheckState.Checked if is_dl else Qt.CheckState.Unchecked)
                    else:
                        item.setCheckState(0, Qt.CheckState.Checked)
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

                    self._file_item_map[f_idx] = item

                    pb = QProgressBar()
                    pb.setRange(0, 100)
                    pb.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    pb.setStyleSheet(progress_style)
                    self._tree_files.setItemWidget(item, 2, pb)

                    if is_torrent:
                        combo = QComboBox()
                        for p_text in ["Max (100%)", "High (75%)", "Medium (50%)", "Low (25%)", "Don't Download"]:
                            combo.addItem(p_text, _PRIORITY_TO_VAL[p_text])
                        prio_label = _priority_to_label(curr_prio)
                        combo.setCurrentText(prio_label)
                        combo.currentIndexChanged.connect(lambda idx, it=item: self._on_file_priority_combo_changed(it))
                        self._tree_files.setItemWidget(item, 3, combo)
                    else:
                        item.setText(3, "Medium (50%)")

        _create_items(self._tree_files, root_nodes)
        self._tree_files.expandAll()
        self._tree_files.blockSignals(False)

    def _get_descendant_file_items(self, item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        files = []
        for i in range(item.childCount()):
            child = item.child(i)
            data = child.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("is_folder"):
                files.extend(self._get_descendant_file_items(child))
            else:
                files.append(child)
        return files

    def _refresh_folder_aggregates(self, folder_item: QTreeWidgetItem, is_torrent: bool):
        descendant_files = self._get_descendant_file_items(folder_item)
        if not descendant_files:
            return

        total_size = 0
        total_downloaded = 0
        checked_count = 0
        all_completed = True
        any_downloading = False
        any_error = False
        priorities = set()

        for it in descendant_files:
            data = it.data(0, Qt.ItemDataRole.UserRole) or {}
            f = data.get("data", {})
            sz = f.get("size", 0)
            total_size += sz
            pct_raw = f.get("progress", 0.0)
            pct = (pct_raw / 100.0) if pct_raw > 1.0 else pct_raw
            total_downloaded += int(sz * pct)

            if it.checkState(0) == Qt.CheckState.Checked:
                checked_count += 1

            st = self._get_file_status(f, self._current_entry)
            if st != "completed":
                all_completed = False
            if st in ("downloading", "fetching_metadata"):
                any_downloading = True
            elif st == "error":
                any_error = True

            priorities.add(f.get("priority", 4))

        folder_item.setText(1, humanize.naturalsize(total_size, binary=True) if total_size > 0 else "—")
        if total_downloaded > 0 and total_size > 0 and total_downloaded < total_size:
            folder_item.setToolTip(1, f"Downloaded: {humanize.naturalsize(total_downloaded, binary=True)} of {folder_item.text(1)}")
        else:
            folder_item.setToolTip(1, f"Size: {folder_item.text(1)}")

        folder_pct = (total_downloaded / total_size * 100.0) if total_size > 0 else 0.0
        pb = self._tree_files.itemWidget(folder_item, 2)
        if isinstance(pb, QProgressBar):
            pb.setTextVisible(True)
            pb.setFormat(f"{folder_pct:.1f}%")
            pb.setValue(int(min(max(folder_pct, 0.0), 100.0)))

        # Update check state
        if checked_count == len(descendant_files):
            new_state = Qt.CheckState.Checked
        elif checked_count == 0:
            new_state = Qt.CheckState.Unchecked
        else:
            new_state = Qt.CheckState.PartiallyChecked
        self._tree_files.blockSignals(True)
        try:
            folder_item.setCheckState(0, new_state)
        finally:
            self._tree_files.blockSignals(False)

        # Update priority combo
        if is_torrent:
            combo = self._tree_files.itemWidget(folder_item, 3)
            if isinstance(combo, QComboBox):
                combo.blockSignals(True)
                if len(priorities) == 1:
                    p_val = next(iter(priorities))
                    combo.setCurrentText(_priority_to_label(p_val))
                else:
                    if combo.findText("Mixed") == -1:
                        combo.addItem("Mixed", -1)
                    combo.setCurrentText("Mixed")
                combo.blockSignals(False)

        # Update status
        if all_completed and len(descendant_files) > 0:
            folder_item.setText(4, "Completed")
        elif any_error:
            folder_item.setText(4, "Error")
        elif any_downloading:
            folder_item.setText(4, "Downloading")
        else:
            folder_item.setText(4, "Pending")

    def _get_file_status(self, f: dict, entry: DownloadEntry) -> str:
        """Derive display status for a torrent or HTTP file from entry status and file progress."""
        if f.get("priority", 4) == 0:
            return "skipped"
        entry_status = entry.status if entry else "pending"
        if entry_status in ("completed", "seeding"):
            return "completed"
        if entry_status == "error":
            return "error"
        pct_raw = f.get("progress", 0.0)
        pct = pct_raw if pct_raw > 1.0 else (pct_raw * 100.0)
        if pct >= 100.0:
            return "completed"
        if pct > 0.0 or f.get("downloaded", 0) > 0 or entry_status == "downloading":
            if entry_status == "paused":
                return "paused"
            if entry_status == "stopped":
                return "stopped"
            return "downloading"
        if entry_status in ("paused", "stopped"):
            return entry_status
        return "pending"

    def _live_file_item(self, file_index):
        """Return the tree item for *file_index*, or ``None`` if it no longer exists.

        ``_file_item_map`` holds ``QTreeWidgetItem`` wrappers that were constructed with a
        C++ parent, so PySide does not own them: when the tree is cleared - including while
        the panel is being destroyed, which still emits ``itemSelectionChanged`` - the C++
        objects are deleted and the wrappers dangle. Touching one raises ``RuntimeError``
        from inside a Qt slot, which surfaces as a hard access violation rather than a
        Python exception. Probing and pruning keeps teardown orderings harmless.
        """
        item = self._file_item_map.get(file_index)
        if item is None:
            return None
        try:
            item.data(0, Qt.ItemDataRole.UserRole)
        except RuntimeError:
            self._file_item_map.pop(file_index, None)
            return None
        return item

    def _update_file_values(self, files: list[dict], is_torrent: bool):
        for f in files:
            f_idx = f.get("index", 0)
            item = self._live_file_item(f_idx)
            if not item:
                continue

            data = item.data(0, Qt.ItemDataRole.UserRole) or {}
            data["data"] = f
            item.setData(0, Qt.ItemDataRole.UserRole, data)

            size_val = f.get("size", 0)
            dl_val = f.get("downloaded", 0)
            size_str = humanize.naturalsize(size_val, binary=True) if size_val > 0 else "—"
            item.setText(1, size_str)
            if dl_val > 0 and size_val > 0 and dl_val < size_val:
                item.setToolTip(1, f"Downloaded: {humanize.naturalsize(dl_val, binary=True)} of {size_str}")
            else:
                item.setToolTip(1, f"Size: {size_str}")

            if size_val > 0 and dl_val >= 0:
                pct_val = min(max((dl_val / size_val) * 100.0, 0.0), 100.0)
            else:
                pct_raw = f.get("progress", 0.0)
                pct_val = pct_raw if pct_raw > 1.0 else (pct_raw * 100.0)
                pct_val = min(max(pct_val, 0.0), 100.0)
            pb = self._tree_files.itemWidget(item, 2)
            if isinstance(pb, QProgressBar):
                pb.setTextVisible(True)
                pb.setFormat(f"{pct_val:.1f}%")
                pb.setValue(int(pct_val))
                dl_str = humanize.naturalsize(dl_val, binary=True) if dl_val > 0 else "0 B"
                pb.setToolTip(f"{pct_val:.1f}% ({dl_str} / {size_str})")

            status_str = self._get_file_status(f, self._current_entry)
            item.setText(4, _to_str(status_str).capitalize())

            if is_torrent:
                curr_prio = f.get("priority", 4)
                combo = self._tree_files.itemWidget(item, 3)
                if isinstance(combo, QComboBox):
                    combo.blockSignals(True)
                    combo.setCurrentText(_priority_to_label(curr_prio))
                    combo.blockSignals(False)
                expected_state = Qt.CheckState.Checked if curr_prio > 0 else Qt.CheckState.Unchecked
                if item.checkState(0) != expected_state:
                    self._tree_files.blockSignals(True)
                    try:
                        item.setCheckState(0, expected_state)
                    finally:
                        self._tree_files.blockSignals(False)

        for folder_item in reversed(self._folder_items):
            self._refresh_folder_aggregates(folder_item, is_torrent)

    def _is_file_downloaded(self, item: QTreeWidgetItem) -> tuple[bool, Optional[Path]]:
        """Return (is_downloaded, disk_path) for a file item."""
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("is_folder"):
            return False, None
        f_data = data.get("data", {})
        file_path_rel = f_data.get("path")
        entry = self._current_entry or (self._manager.get_entry(self._download_id) if self._download_id else None)
        disk_path = None
        if entry and entry.save_path and file_path_rel:
            disk_path = Path(entry.save_path) / file_path_rel

        disk_exists = disk_path.exists() if disk_path else False
        dl_bytes = f_data.get("downloaded", 0)
        pct_raw = f_data.get("progress", 0.0)
        pct = pct_raw if pct_raw > 1.0 else (pct_raw * 100.0)

        is_dl = disk_exists or dl_bytes > 0 or pct >= 100.0
        return is_dl, disk_path

    def _reset_file_item_trashed(self, f_it: QTreeWidgetItem):
        """Reset progress and status display for a trashed file item."""
        data = f_it.data(0, Qt.ItemDataRole.UserRole) or {}
        f_data = data.get("data")
        if isinstance(f_data, dict):
            f_data["downloaded"] = 0
            f_data["progress"] = 0.0
            f_data["status"] = "skipped"
            f_data["priority"] = 0
        pb = self._tree_files.itemWidget(f_it, 2)
        if isinstance(pb, QProgressBar):
            pb.setValue(0)
            pb.setToolTip("0.0% (0 B)")
        f_it.setText(4, "Skipped")
        size_str = f_it.text(1)
        f_it.setToolTip(1, f"Size: {size_str}")

    def _confirm_and_trash_items(self, items: list[QTreeWidgetItem]) -> bool:
        """Confirm and trash downloaded files for a set of file or folder tree items.

        Consolidates confirmation into a single dialog so selecting multiple files
        or a folder never prompts more than once. Unlocks files prior to trashing.
        Returns True if proceeding (or no files needed trashing), False if canceled.
        """
        all_descendants: list[QTreeWidgetItem] = []
        for it in items:
            data = it.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("is_folder", False):
                all_descendants.extend(self._get_descendant_file_items(it))
            else:
                all_descendants.append(it)

        # De-duplicate while preserving order
        unique_file_items: list[QTreeWidgetItem] = []
        seen = set()
        for f_it in all_descendants:
            if f_it not in seen:
                seen.add(f_it)
                unique_file_items.append(f_it)

        dl_items: list[tuple[QTreeWidgetItem, Optional[Path]]] = []
        for f_it in unique_file_items:
            f_is_dl, f_disk_path = self._is_file_downloaded(f_it)
            if f_is_dl:
                dl_items.append((f_it, f_disk_path))

        if not dl_items:
            return True

        if len(dl_items) == 1 and len(items) == 1 and not (items[0].data(0, Qt.ItemDataRole.UserRole) or {}).get("is_folder", False):
            f_it, _ = dl_items[0]
            file_name = f_it.text(0).lstrip("📄 ").strip()
            title = "Move Downloaded File to Trash?"
            msg = (
                f"'{file_name}' has already been downloaded (or partially downloaded).\n\n"
                "Setting it to 'Don't Download' will move the downloaded file to the Trash / Recycle Bin.\n\n"
                "Do you want to continue?"
            )
        elif len(items) == 1 and (items[0].data(0, Qt.ItemDataRole.UserRole) or {}).get("is_folder", False):
            folder_name = items[0].text(0).lstrip("📁 ").strip()
            title = "Move Downloaded Files to Trash?"
            msg = (
                f"Folder '{folder_name}' contains {len(dl_items)} downloaded (or partially downloaded) file(s).\n\n"
                "Setting this folder to 'Don't Download' will move these files to the Trash / Recycle Bin.\n\n"
                "Do you want to continue?"
            )
        else:
            title = "Move Downloaded Files to Trash?"
            msg = (
                f"There are {len(dl_items)} downloaded (or partially downloaded) files selected.\n\n"
                "Setting them to 'Don't Download' will move these files to the Trash / Recycle Bin.\n\n"
                "Do you want to continue?"
            )

        reply = QMessageBox.question(
            self,
            title,
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

        for f_it, f_disk_path in dl_items:
            if f_disk_path and f_disk_path.exists():
                unlock_path(f_disk_path)
                send_to_trash(f_disk_path)
            self._reset_file_item_trashed(f_it)

        return True

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int):
        if column != 0 or self._tree_updating or not self._download_id:
            return
        new_state = item.checkState(0)
        if new_state == Qt.CheckState.PartiallyChecked:
            return

        self._tree_updating = True
        try:
            data = item.data(0, Qt.ItemDataRole.UserRole) or {}
            is_folder = data.get("is_folder", False)
            is_checked = (new_state == Qt.CheckState.Checked)

            if not is_checked:
                # If folder and all descendants are already unchecked, nothing to prompt or trash
                if is_folder:
                    descendants = self._get_descendant_file_items(item)
                    if all(f_it.checkState(0) == Qt.CheckState.Unchecked for f_it in descendants):
                        return

                # Unchecking file or folder: check for downloaded files to confirm trashing
                selected = self._tree_files.selectedItems()
                items_to_confirm = selected if (item in selected and len(selected) > 1) else [item]
                if not self._confirm_and_trash_items(items_to_confirm):
                    self._tree_files.blockSignals(True)
                    try:
                        item.setCheckState(0, Qt.CheckState.Checked)
                    finally:
                        self._tree_files.blockSignals(False)
                    return

            if is_folder:
                descendants = self._get_descendant_file_items(item)
                self._tree_files.blockSignals(True)
                try:
                    for f_it in descendants:
                        f_it.setCheckState(0, Qt.CheckState.Checked if is_checked else Qt.CheckState.Unchecked)
                finally:
                    self._tree_files.blockSignals(False)

                for f_it in descendants:
                    f_data = f_it.data(0, Qt.ItemDataRole.UserRole) or {}
                    f_idx = f_data.get("file_index")
                    prio = 4 if is_checked else 0
                    if "data" in f_data and isinstance(f_data["data"], dict):
                        f_data["data"]["priority"] = prio
                    combo = self._tree_files.itemWidget(f_it, 3)
                    if isinstance(combo, QComboBox):
                        combo.blockSignals(True)
                        combo.setCurrentText("Medium (50%)" if is_checked else "Don't Download")
                        combo.blockSignals(False)
                    if f_idx is not None:
                        self._manager.set_torrent_file_priority(self._download_id, f_idx, prio)
            else:
                f_idx = data.get("file_index")
                prio = 4 if is_checked else 0
                if "data" in data and isinstance(data["data"], dict):
                    data["data"]["priority"] = prio
                combo = self._tree_files.itemWidget(item, 3)
                if isinstance(combo, QComboBox):
                    combo.blockSignals(True)
                    combo.setCurrentText("Medium (50%)" if is_checked else "Don't Download")
                    combo.blockSignals(False)
                if f_idx is not None:
                    self._manager.set_torrent_file_priority(self._download_id, f_idx, prio)

            for fld in reversed(self._folder_items):
                self._refresh_folder_aggregates(fld, is_torrent=True)
        finally:
            self._tree_updating = False

    def _on_tree_item_double_clicked(self, item: QTreeWidgetItem):
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("is_folder", False):
            return
        if not self._current_entry:
            return
        file_path = data.get("data", {}).get("path")
        if not file_path:
            return
        full_path = str(Path(self._current_entry.save_path) / file_path)
        if Path(full_path).exists():
            # Was `os.startfile`, which only exists on Windows — an AttributeError everywhere
            # else. create_if_missing=False because the path is known to exist here, and the
            # default would fabricate a placeholder if it vanished between the check and the call.
            from my_idm.external_tools import open_file_in_default_app

            open_file_in_default_app(full_path, create_if_missing=False)
        else:
            self._manager.mark_file_not_found(self._current_entry.id)

    def _on_file_priority_combo_changed(self, item: QTreeWidgetItem):
        if self._tree_updating or not self._download_id:
            return
        combo = self._tree_files.itemWidget(item, 3)
        if not isinstance(combo, QComboBox):
            return
        prio_val = combo.currentData()
        if prio_val is None or prio_val < 0:
            return

        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        f_idx = data.get("file_index")
        if f_idx is None:
            return

        if prio_val == 0:
            if not self._confirm_and_trash_items([item]):
                curr_prio = data.get("data", {}).get("priority", 4)
                if curr_prio == 0:
                    curr_prio = 4
                combo.blockSignals(True)
                combo.setCurrentText(_priority_to_label(curr_prio))
                combo.blockSignals(False)
                return

        self._tree_updating = True
        try:
            new_state = Qt.CheckState.Checked if prio_val > 0 else Qt.CheckState.Unchecked
            self._tree_files.blockSignals(True)
            try:
                item.setCheckState(0, new_state)
            finally:
                self._tree_files.blockSignals(False)
            if "data" in data and isinstance(data["data"], dict):
                data["data"]["priority"] = prio_val
            self._manager.set_torrent_file_priority(self._download_id, f_idx, prio_val)
            for fld in reversed(self._folder_items):
                self._refresh_folder_aggregates(fld, is_torrent=True)
        finally:
            self._tree_updating = False

    def _on_folder_priority_changed(self, item: QTreeWidgetItem):
        if self._tree_updating or not self._download_id:
            return
        combo = self._tree_files.itemWidget(item, 3)
        if not isinstance(combo, QComboBox):
            return
        prio_val = combo.currentData()
        if prio_val is None or prio_val < 0:
            return

        descendants = self._get_descendant_file_items(item)
        if prio_val == 0:
            if not self._confirm_and_trash_items([item]):
                combo.blockSignals(True)
                combo.setCurrentText("Medium (50%)")
                combo.blockSignals(False)
                return

        self._tree_updating = True
        try:
            for f_it in descendants:
                f_data = f_it.data(0, Qt.ItemDataRole.UserRole) or {}
                f_idx = f_data.get("file_index")
                new_state = Qt.CheckState.Checked if prio_val > 0 else Qt.CheckState.Unchecked
                f_it.setCheckState(0, new_state)
                f_combo = self._tree_files.itemWidget(f_it, 3)
                if isinstance(f_combo, QComboBox):
                    f_combo.blockSignals(True)
                    f_combo.setCurrentText(_priority_to_label(prio_val))
                    f_combo.blockSignals(False)
                if f_idx is not None:
                    self._manager.set_torrent_file_priority(self._download_id, f_idx, prio_val)

            for fld in reversed(self._folder_items):
                self._refresh_folder_aggregates(fld, is_torrent=True)
        finally:
            self._tree_updating = False

    def _show_files_context_menu(self, pos):
        item = self._tree_files.itemAt(pos)
        if not item or not self._download_id:
            return
        entry = self._manager.get_entry(self._download_id)
        if not entry or entry.download_type != "torrent":
            return

        selected_items = self._tree_files.selectedItems()
        if not selected_items or item not in selected_items:
            selected_items = [item]

        menu = QMenu(self)
        prio_menu = menu.addMenu("Bandwidth Allocation / Priority")
        options = [
            ("Max (100%)", 7),
            ("High (75%)", 6),
            ("Medium (50%)", 4),
            ("Low (25%)", 1),
            ("Don't Download", 0),
        ]
        combo = self._tree_files.itemWidget(item, 3)
        curr_text = combo.currentText() if isinstance(combo, QComboBox) else ""

        for text, val in options:
            act = prio_menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(curr_text == text)
            act.triggered.connect(lambda checked=False, v=val, its=selected_items: self._set_items_priority(its, v))

        menu.exec(self._tree_files.viewport().mapToGlobal(pos))

    def _set_items_priority(self, items: list[QTreeWidgetItem], priority_val: int):
        if not self._download_id or not items:
            return
        if priority_val == 0:
            if not self._confirm_and_trash_items(items):
                return
        self._tree_updating = True
        try:
            for item in items:
                data = item.data(0, Qt.ItemDataRole.UserRole) or {}
                if data.get("is_folder", False):
                    descendants = self._get_descendant_file_items(item)
                    item.setCheckState(0, Qt.CheckState.Checked if priority_val > 0 else Qt.CheckState.Unchecked)
                    combo = self._tree_files.itemWidget(item, 3)
                    if isinstance(combo, QComboBox):
                        combo.blockSignals(True)
                        combo.setCurrentText(_priority_to_label(priority_val))
                        combo.blockSignals(False)
                    for f_it in descendants:
                        f_data = f_it.data(0, Qt.ItemDataRole.UserRole) or {}
                        f_idx = f_data.get("file_index")
                        f_it.setCheckState(0, Qt.CheckState.Checked if priority_val > 0 else Qt.CheckState.Unchecked)
                        f_combo = self._tree_files.itemWidget(f_it, 3)
                        if isinstance(f_combo, QComboBox):
                            f_combo.blockSignals(True)
                            f_combo.setCurrentText(_priority_to_label(priority_val))
                            f_combo.blockSignals(False)
                        if "data" in f_data and isinstance(f_data["data"], dict):
                            f_data["data"]["priority"] = priority_val
                        if f_idx is not None:
                            self._manager.set_torrent_file_priority(self._download_id, f_idx, priority_val)
                else:
                    f_data = data
                    f_idx = f_data.get("file_index")
                    item.setCheckState(0, Qt.CheckState.Checked if priority_val > 0 else Qt.CheckState.Unchecked)
                    combo = self._tree_files.itemWidget(item, 3)
                    if isinstance(combo, QComboBox):
                        combo.blockSignals(True)
                        combo.setCurrentText(_priority_to_label(priority_val))
                        combo.blockSignals(False)
                    if "data" in f_data and isinstance(f_data["data"], dict):
                        f_data["data"]["priority"] = priority_val
                    if f_idx is not None:
                        self._manager.set_torrent_file_priority(self._download_id, f_idx, priority_val)

            for fld in reversed(self._folder_items):
                self._refresh_folder_aggregates(fld, is_torrent=True)
        finally:
            self._tree_updating = False

    def _set_item_priority(self, item: QTreeWidgetItem, priority_val: int):
        self._set_items_priority([item], priority_val)

    def _on_row_checkbox_toggled(self, row: int, checked: bool):
        if not self._download_id:
            return
        item = self._live_file_item(row)
        if item is not None:
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self._on_tree_item_changed(item, 0)

    def _on_row_priority_changed(self, row: int):
        if not self._download_id:
            return
        item = self._live_file_item(row)
        if item is not None:
            self._on_file_priority_combo_changed(item)

    def _on_file_checkbox_toggled(self, file_index: int, checked: bool, combo: Optional[QComboBox]):
        if not self._download_id:
            return
        prio = 4 if checked else 0
        if combo:
            combo.blockSignals(True)
            combo.setCurrentText("Normal" if checked else "Don't Download")
            combo.blockSignals(False)
        self._manager.set_torrent_file_priority(self._download_id, file_index, prio)

    def _on_file_priority_changed(self, file_index: int, combo: QComboBox, chk: Optional[QCheckBox] = None):
        if not self._download_id:
            return
        prio_val = combo.currentData()
        if prio_val is not None:
            if chk:
                chk.blockSignals(True)
                chk.setChecked(prio_val > 0)
                chk.blockSignals(False)
            self._manager.set_torrent_file_priority(self._download_id, file_index, prio_val)

    def _update_peers(self, entry: DownloadEntry):
        if entry.download_type != "torrent":
            self._lbl_peers_status.setText("Peer and swarm monitoring is only active for BitTorrent transfers.")
            self._table_peers.setRowCount(0)
            return

        peers = self._manager.get_torrent_peers(entry.id)
        if not isinstance(peers, list):
            peers = []
        ts = to_int(getattr(entry, "total_seeds", 0)) or (to_int(entry.metadata.get("total_seeds", 0)) if entry.metadata else 0)
        tp = to_int(getattr(entry, "total_peers", 0)) or (to_int(entry.metadata.get("total_peers", 0)) if entry.metadata else 0)
        swarm_str = f" ({ts} seeds, {tp} peers in swarm)" if (ts > 0 or tp > 0) else ""
        self._lbl_peers_status.setText(f"{len(peers)} connected peer(s) in active swarm{swarm_str}")

        rebuild = self._table_peers.rowCount() != len(peers)
        if rebuild:
            self._table_peers.setRowCount(len(peers))

        for row, p in enumerate(peers):
            if not isinstance(p, dict):
                continue
            # IP : Port
            ip_item = self._table_peers.item(row, 0)
            if not ip_item:
                ip_item = QTableWidgetItem()
                self._table_peers.setItem(row, 0, ip_item)
            ip_item.setText(_to_str(p.get("ip", "—")))

            # Client
            client_item = self._table_peers.item(row, 1)
            if not client_item:
                client_item = QTableWidgetItem()
                self._table_peers.setItem(row, 1, client_item)
            c_name = _to_str(p.get("client", "Unknown")).strip()
            client_item.setText(c_name if c_name else "Unknown")

            # Progress
            prog_item = self._table_peers.item(row, 2)
            if not prog_item:
                prog_item = QTableWidgetItem()
                prog_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_peers.setItem(row, 2, prog_item)
            prog_val = p.get('progress', 0.0)
            # Handle either 0.0-1.0 or 0-100 float
            if prog_val > 1.0:
                prog_pct = prog_val
            else:
                prog_pct = prog_val * 100.0
            prog_item.setText(f"{prog_pct:.1f}%")

            # Down Speed
            down_item = self._table_peers.item(row, 3)
            if not down_item:
                down_item = QTableWidgetItem()
                down_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table_peers.setItem(row, 3, down_item)
            down_item.setText(_format_speed(p.get("down_speed", 0.0)))

            # Up Speed
            up_item = self._table_peers.item(row, 4)
            if not up_item:
                up_item = QTableWidgetItem()
                up_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table_peers.setItem(row, 4, up_item)
            up_item.setText(_format_speed(p.get("up_speed", 0.0)))

            # Flags
            flags_item = self._table_peers.item(row, 5)
            if not flags_item:
                flags_item = QTableWidgetItem()
                self._table_peers.setItem(row, 5, flags_item)
            flags_item.setText(_to_str(p.get("flags", "")))

    def _update_trackers(self, entry: DownloadEntry):
        if entry.download_type != "torrent":
            self._lbl_trackers_status.setText("Trackers are only used for BitTorrent downloads.")
            self._table_trackers.setRowCount(0)
            return

        trackers = self._manager.get_torrent_trackers(entry.id)
        if not isinstance(trackers, list):
            trackers = []
        self._lbl_trackers_status.setText(f"{len(trackers)} tracker(s) announced")

        rebuild = self._table_trackers.rowCount() != len(trackers)
        if rebuild:
            self._table_trackers.setRowCount(len(trackers))

        for row, t in enumerate(trackers):
            # Tier
            tier_item = self._table_trackers.item(row, 0)
            if not tier_item:
                tier_item = QTableWidgetItem()
                tier_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_trackers.setItem(row, 0, tier_item)
            tier_item.setText(_to_str(t.get("tier", 0)))

            # URL
            url_item = self._table_trackers.item(row, 1)
            if not url_item:
                url_item = QTableWidgetItem()
                self._table_trackers.setItem(row, 1, url_item)
            url_item.setText(_to_str(t.get("url", "")))

            # Status
            status_item = self._table_trackers.item(row, 2)
            if not status_item:
                status_item = QTableWidgetItem()
                self._table_trackers.setItem(row, 2, status_item)
            status_item.setText(_to_str(t.get("status", "Working")))

            # Send Stats
            stats_item = self._table_trackers.item(row, 3)
            if not stats_item:
                stats_item = QTableWidgetItem()
                stats_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_trackers.setItem(row, 3, stats_item)
            stats_item.setText("Yes" if t.get("send_stats") else "No")

    def _update_segments(self, entry: DownloadEntry):
        if entry.download_type == "torrent":
            self._lbl_segments_status.setText("BitTorrent divides transfers across peer pieces rather than fixed byte ranges.")
            self._table_segments.setRowCount(0)
            return

        segments = self._manager.get_download_segments(entry.id)
        self._lbl_segments_status.setText(f"{len(segments)} segment(s) configured for parallel download")

        rebuild = self._table_segments.rowCount() != len(segments)
        if rebuild:
            self._table_segments.setRowCount(len(segments))

        for row, s in enumerate(segments):
            # Index
            idx_item = self._table_segments.item(row, 0)
            if not idx_item:
                idx_item = QTableWidgetItem()
                idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_segments.setItem(row, 0, idx_item)
            idx_item.setText(f"Segment #{s.index + 1}")

            # Byte Range
            range_str = f"{s.start_byte:,} – {s.end_byte:,}" if s.end_byte > 0 else f"{s.start_byte:,} – end"
            range_item = self._table_segments.item(row, 1)
            if not range_item:
                range_item = QTableWidgetItem()
                self._table_segments.setItem(row, 1, range_item)
            range_item.setText(range_str)

            # Downloaded
            dl_str = humanize.naturalsize(s.downloaded_bytes, binary=True)
            dl_item = self._table_segments.item(row, 2)
            if not dl_item:
                dl_item = QTableWidgetItem()
                dl_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table_segments.setItem(row, 2, dl_item)
            dl_item.setText(dl_str)

            # Progress bar
            seg_len = (s.end_byte - s.start_byte + 1) if s.end_byte >= s.start_byte else 0
            pct = int((s.downloaded_bytes / seg_len * 100)) if seg_len > 0 else 0
            pct = min(100, max(0, pct))

            prog_bar = self._table_segments.cellWidget(row, 3)
            if not isinstance(prog_bar, QProgressBar):
                prog_bar = QProgressBar()
                prog_bar.setRange(0, 100)
                prog_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
                prog_bar.setStyleSheet(
                    f"QProgressBar {{ border: 1px solid {Colors.BORDER}; border-radius: 3px; background: {Colors.BG_DARK}; height: 16px; text-align: center; font-size: 11px; }} "
                    f"QProgressBar::chunk {{ background: {Colors.GREEN}; border-radius: 2px; }}"
                )
                self._table_segments.setCellWidget(row, 3, prog_bar)
            prog_bar.setValue(pct)

            # Status
            status_item = self._table_segments.item(row, 4)
            if not status_item:
                status_item = QTableWidgetItem()
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table_segments.setItem(row, 4, status_item)
            status_item.setText(_to_str(s.status).capitalize())

    # -- Actions --------------------------------------------------------------

    def _on_open_folder_clicked(self):
        from my_idm.external_tools import show_in_folder

        if hasattr(self, "_tab_console") and self._tabs.currentWidget() == self._tab_console:
            repo = self._manager.external_tools_config.get_effective_repo_path()
            if repo and Path(repo).exists():
                show_in_folder(repo)
            return

        if not self._current_entry:
            return
        folder = self._current_entry.save_path
        file_path = self._current_entry.file_path
        if file_path and Path(file_path).exists():
            # Hands the file over so the platform can *select* it, which is what the Windows
            # `explorer /select,` branch was reaching for. The old non-Windows branch opened the
            # containing folder instead, and the Windows branch passed `/select,` as a separate
            # argv element, which Explorer only tolerates by accident.
            show_in_folder(file_path)
        elif folder and Path(folder).exists():
            show_in_folder(folder)

    # -- AnimePahe Console Log View & Streaming -------------------------------

    def _create_console_view(self) -> QWidget:
        widget = QWidget(self)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Tab widget inside Console view: Log and Embedded Browser
        self._console_subtabs = QTabWidget(widget)
        self._console_log_tab = self._build_console_log_tab()
        self._browser_tab = self._create_browser_tab()

        self._console_subtabs.addTab(self._console_log_tab, "📄 Log")
        # _browser_tab is initially hidden and will be added dynamically when the browser opens

        layout.addWidget(self._console_subtabs)
        return widget

    def _build_console_log_tab(self) -> QWidget:
        widget = QWidget(self)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Control Bar
        ctrl_bar = QHBoxLayout()
        ctrl_bar.setSpacing(8)

        is_running = self._manager.is_animepahe_running()
        self._console_status_lbl = QLabel("● Active" if is_running else "○ Stopped", widget)
        self._console_status_lbl.setStyleSheet(
            "color: #50fa7b; font-weight: bold; font-size: 11px;"
            if is_running else
            "color: #ff5555; font-weight: bold; font-size: 11px;"
        )
        ctrl_bar.addWidget(self._console_status_lbl)

        self._console_info_lbl = QLabel("", widget)
        self._console_info_lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: 11px;")
        ctrl_bar.addWidget(self._console_info_lbl)

        ctrl_bar.addStretch(1)

        # Filter box
        self._console_filter_edit = QLineEdit(widget)
        self._console_filter_edit.setPlaceholderText("🔍 Filter logs...")
        self._console_filter_edit.setClearButtonEnabled(True)
        self._console_filter_edit.setFixedWidth(180)
        self._console_filter_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {Colors.BG_DARK};
                color: {Colors.TEXT};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 11px;
            }}
            QLineEdit:focus {{
                border-color: {Colors.ACCENT};
            }}
        """)
        self._console_filter_edit.textChanged.connect(self._on_console_filter_changed)
        ctrl_bar.addWidget(self._console_filter_edit)

        # Auto-scroll checkbox
        self._console_autoscroll_cb = QCheckBox("Auto-scroll", widget)
        self._console_autoscroll_cb.setChecked(True)
        self._console_autoscroll_cb.setStyleSheet(f"color: {Colors.TEXT}; font-size: 11px;")
        ctrl_bar.addWidget(self._console_autoscroll_cb)

        # Wrap lines checkbox
        self._console_wrap_cb = QCheckBox("Wrap", widget)
        self._console_wrap_cb.setChecked(False)
        self._console_wrap_cb.setStyleSheet(f"color: {Colors.TEXT}; font-size: 11px;")
        self._console_wrap_cb.toggled.connect(self._on_console_wrap_toggled)
        ctrl_bar.addWidget(self._console_wrap_cb)

        # Clear button
        self._console_clear_btn = QPushButton("🗑️ Clear", widget)
        self._console_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._console_clear_btn.setToolTip("Clear the displayed log output")
        self._console_clear_btn.clicked.connect(self._on_clear_console_clicked)
        ctrl_bar.addWidget(self._console_clear_btn)

        # Open file button
        self._console_open_btn = QPushButton("📄 Open File", widget)
        self._console_open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._console_open_btn.setToolTip("Open console_log.txt in external text editor")
        self._console_open_btn.clicked.connect(self._on_open_console_file_clicked)
        ctrl_bar.addWidget(self._console_open_btn)

        # Stop / Start Scraper button
        self._console_action_btn = QPushButton("⏹️ Stop Scraper" if is_running else "▶️ Start Scraper", widget)
        self._console_action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._console_action_btn.clicked.connect(self._on_toggle_scraper_clicked)
        ctrl_bar.addWidget(self._console_action_btn)

        # Open GUI button
        self._console_open_gui_btn = QPushButton("🎬 Open GUI", widget)
        self._console_open_gui_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._console_open_gui_btn.setToolTip("Open AnimePahe desktop GUI")
        self._console_open_gui_btn.clicked.connect(self._on_open_animepahe_gui_clicked)
        ctrl_bar.addWidget(self._console_open_gui_btn)

        layout.addLayout(ctrl_bar)

        # Session list on the left, log text on the right. The text stays a
        # QPlainTextEdit so a user can drag-select several lines and press Ctrl+C.
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._console_session_tabs = SessionTabList(widget)
        body.addWidget(self._console_session_tabs)

        self._console_text = QPlainTextEdit(widget)
        self._console_text.setReadOnly(True)
        self._console_text.setMaximumBlockCount(15000)
        self._console_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = fonts.mono_font(10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._console_text.setFont(font)
        self._console_text.setStyleSheet("""
            QPlainTextEdit {
                background-color: #12151b;
                color: #d1d5db;
                border: 1px solid #28303e;
                border-radius: 4px;
                padding: 6px;
                selection-background-color: #2b3d5b;
                selection-color: #ffffff;
            }
        """)
        body.addWidget(self._console_text, stretch=1)
        layout.addLayout(body, stretch=1)

        self._console_session_tabs.currentKeyChanged.connect(self._on_console_session_selected)

        return widget

    def _create_browser_tab(self) -> QWidget:
        widget = QWidget(self._console_subtabs)
        widget.hide()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Header Info Bar
        info_bar = QHBoxLayout()
        info_bar.setContentsMargins(4, 2, 4, 2)
        info_bar.setSpacing(8)

        self._browser_status_lbl = QLabel("● Active", widget)
        self._browser_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold; font-size: 11px;")
        info_bar.addWidget(self._browser_status_lbl)

        lbl_desc = QLabel("Real-time Cloudflare bypass & link extraction view (undetected-chromedriver)", widget)
        lbl_desc.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: 11px;")
        info_bar.addWidget(lbl_desc)

        info_bar.addStretch(1)

        self._btn_float_browser = QPushButton("↗ Detach Window", widget)
        self._btn_float_browser.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_float_browser.setToolTip("Detach the browser to float on desktop or re-embed")
        self._btn_float_browser.clicked.connect(self._on_toggle_float_browser)
        info_bar.addWidget(self._btn_float_browser)

        layout.addLayout(info_bar)

        # Embedded browser container
        self._browser_container = EmbeddedBrowserContainer(widget)
        self._browser_container.hide()
        layout.addWidget(self._browser_container, stretch=1)

        return widget

    def _on_toggle_float_browser(self):
        """Toggle floating the browser window outside the container or re-docking it."""
        if not self.is_browser_attached():
            return
        if self._is_browser_floating:
            # Re-dock
            if self._browser_container.chrome_hwnd:
                self._browser_container.attach_window(self._browser_container.chrome_hwnd)
            self._is_browser_floating = False
            self._btn_float_browser.setText("↗ Detach Window")
            self._btn_float_browser.setToolTip("Detach the browser to float on desktop")
        else:
            # Float
            if sys.platform == "win32":
                try:
                    import ctypes
                    user32 = ctypes.windll.user32
                    hwnd = self._browser_container.chrome_hwnd
                    if hwnd and user32.IsWindow(hwnd):
                        user32.SetParent(ctypes.c_void_p(hwnd), None)
                        style = user32.GetWindowLongW(hwnd, -16)
                        style |= (0x00C00000 | 0x00040000 | 0x80000000)
                        style &= ~0x40000000
                        if hasattr(user32, "SetWindowLongPtrW"):
                            user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), -16, ctypes.c_ssize_t(style))
                        else:
                            user32.SetWindowLongW(ctypes.c_void_p(hwnd), -16, ctypes.c_long(style))
                        user32.ShowWindow(ctypes.c_void_p(hwnd), 5)
                except Exception as exc:
                    logger.debug("Error floating browser window: %s", exc)
            self._is_browser_floating = True
            self._btn_float_browser.setText("↙ Embed Window")
            self._btn_float_browser.setToolTip("Dock the browser window back inside the panel")

    def _on_details_tab_changed(self, index: int):
        if hasattr(self, "_tab_queues") and self._tabs.widget(index) == self._tab_queues:
            self._update_queues_header()
            self._update_queues()
            return
        if self._current_entry:
            self._update_header(self._current_entry)
            self.refresh()
        else:
            self._clear_header()

    def _on_mode_tab_changed(self, index: int):
        self._mode_stack.setCurrentIndex(index)
        mode = "console" if index == 1 else "details"
        if mode == "console":
            self._update_console_header()
            if self.isVisible():
                self._start_log_timer()
        else:
            self._stop_log_timer()
            if self._current_entry:
                self._update_header(self._current_entry)
            else:
                self._clear_header()
        self.mode_changed.emit(mode)

    def current_mode(self) -> str:
        """Returns 'details' or 'console' based on active left-side tab."""
        return "console" if self._side_tabs.currentIndex() == 1 else "details"

    def set_mode(self, mode: str):
        """Switch left-side tab mode ('details' or 'console')."""
        idx = 1 if mode == "console" else 0
        self._side_tabs.setCurrentIndex(idx)

    def _start_log_timer(self):
        if not self._log_timer.isActive():
            self._log_timer.start(250)
        self._prune_console_log()
        self._poll_console_log()

    def _stop_log_timer(self):
        if self._log_timer.isActive():
            self._log_timer.stop()

    # -- Retention -----------------------------------------------------------
    #
    # The scraper appends to console_log.txt forever, so it grows without bound.
    # Only the last CONSOLE_LOG_RETENTION_DAYS days of sessions are kept.

    CONSOLE_LOG_RETENTION_DAYS = 3

    def _session_within_retention(self, header_lines) -> bool:
        """True when a session is inside the retention window (or undated)."""
        if not header_lines:
            return True
        title = self._session_title(header_lines)
        try:
            stamp = datetime.fromisoformat(title.strip())
        except (TypeError, ValueError):
            return True  # undated preamble: keep it rather than guess
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp >= self._retention_cutoff()

    @classmethod
    def _retention_cutoff(cls) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=cls.CONSOLE_LOG_RETENTION_DAYS)

    def _prune_console_log(self) -> None:
        """Drop sessions older than the retention window, on disk and in memory.

        Rewrites the log file so it stops growing. Only safe while the scraper is
        idle; a running scraper holds the file open for append, so the on-disk
        rewrite is skipped in that case and the in-memory drop still applies.
        """
        if not self._raw_log_lines:
            return

        sessions = self._console_sessions()
        keep = [s for s in sessions if self._session_within_retention(s[1])]
        if len(keep) == len(sessions):
            return

        dropped = len(sessions) - len(keep)
        rebuilt: list[str] = []
        for _key, header, body in keep:
            rebuilt.extend(header)
            rebuilt.extend(body)
        if not rebuilt:
            return
        rebuilt.append("\n")

        self._raw_log_lines = rebuilt
        logger.info(
            "Console log retention: dropped %d session(s) older than %d days",
            dropped, self.CONSOLE_LOG_RETENTION_DAYS,
        )

        if self._manager.is_animepahe_running():
            return  # scraper owns the file right now

        log_path = self._manager.external_tools_config.get_console_log_path()
        if not log_path or not log_path.is_file():
            return
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write("".join(rebuilt))
            self._log_offset = os.path.getsize(log_path)
        except OSError as exc:
            logger.debug("Could not compact console log: %s", exc)

        # Refresh the session list so the dropped runs disappear from the tabs.
        self._render_console()

    def _poll_console_log(self):
        log_path = self._manager.external_tools_config.get_console_log_path()
        if not log_path or not log_path.is_file():
            return

        try:
            file_size = os.path.getsize(log_path)
            if file_size < self._log_offset:
                # File truncated or restarted
                self._log_offset = 0
                self._raw_log_lines.clear()
                self._console_text.clear()

            if file_size > self._log_offset:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._log_offset)
                    new_data = f.read()
                    self._log_offset = f.tell()

                if new_data:
                    self._append_log_text(new_data)
        except Exception as exc:
            logger.debug("Error reading console log: %s", exc)

    # -- console session grouping -------------------------------------------
    #
    # The scraper writes a banner before every run:
    #
    #   =======================================================
    #     AnimePahe CLI Scraper Session Started: 2026-09-29 16:18:01
    #     Command: <argv>
    #   =======================================================
    #
    # Those four lines are consumed as a collapsible group header; everything
    # after them is that session's output.

    _SESSION_RULE_CHARS = "="

    @classmethod
    def _is_session_rule(cls, line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and set(stripped) == {cls._SESSION_RULE_CHARS}

    @classmethod
    def _split_sessions(cls, lines):
        """Split raw log lines into ``(header_lines, body_lines)`` per session.

        A session is introduced by the banner line::

            AnimePahe CLI Scraper Session Started: <timestamp>

        optionally preceded by a rule line. Everything from that banner up to
        the next banner is one session; the banner's own header is the optional
        preceding rule plus the banner line itself.

        The scraper writes the banner *without* a closing rule after the Command
        line, so relying on rule lines alone was wrong: the banner lines were
        absorbed into the previous session's body and surfaced as a headerless
        "Earlier output" session, losing the timestamp. The banner line itself
        is the reliable marker, so sessions are keyed on it rather than on
        rules.
        """
        sessions: list[tuple[list[str], list[str]]] = []
        current_header: list[str] = []
        current_body: list[str] = []
        seen_first_banner = False
        carry: str = ""
        preamble: list[str] = []

        i = 0
        n = len(lines)
        while i < n:
            line = carry + lines[i]
            carry = ""
            # A rule that is not newline-terminated is incomplete (the rest of
            # it is in the next chunk). Hold it rather than treating it as a
            # complete rule, which would otherwise open a phantom block.
            if cls._is_session_rule(line) and not line.endswith("\n"):
                carry = line
                i += 1
                continue

            if "Session Started:" in line:
                if carry:
                    # The carried incomplete rule is the banner's opening
                    # rule; it belongs in the header with this banner line.
                    header = [carry, line]
                else:
                    start = i
                    if i > 0 and cls._is_session_rule(lines[i - 1]):
                        start = i - 1
                    header = lines[start:i + 1]
                if seen_first_banner:
                    sessions.append((current_header, current_body))
                elif preamble:
                    # Anything before the first banner is undated preamble. It
                    # is its own headerless session (empty header, content in
                    # the body), kept rather than dropped on a guess, and
                    # surfaced as an "Earlier output" tab. The banner's own
                    # preceding rule is never part of it.
                    pre = list(preamble)
                    while pre and cls._is_session_rule(pre[-1]):
                        pre.pop()
                    if any(line.strip() for line in pre):
                        sessions.append(([], pre))
                    seen_first_banner = True
                else:
                    seen_first_banner = True
                current_header = header
                current_body = []
                carry = ""
                i += 1
                continue
            (current_body if seen_first_banner else preamble).append(line)
            i += 1

        sessions.append((current_header, current_body))
        # A log with no banner at all is itself one undated preamble session.
        if not seen_first_banner and any(line.strip() for line in preamble):
            sessions.append(([], preamble))
        return sessions

    @classmethod
    def _session_key(cls, header_lines, ordinal: int) -> str:
        """Stable identity for a session.

        Two runs can start within the same second, so the timestamp alone is not
        a unique key; the ordinal disambiguates them.
        """
        return f"{cls._session_title(header_lines)}#{ordinal}"

    @staticmethod
    def _session_title(header_lines) -> str:
        """Derive a group label from the banner, e.g. '2026-09-29 16:18:01'."""
        for line in header_lines:
            if "Session Started:" in line:
                return line.split("Session Started:", 1)[1].strip()
        for line in header_lines:
            if "Command:" in line:
                return line.split("Command:", 1)[1].strip()[:60]
        return "Earlier output"

    @staticmethod
    def _session_command(header_lines) -> str:
        for line in header_lines:
            if "Command:" in line:
                return line.split("Command:", 1)[1].strip()
        return ""

    def _console_sessions(self):
        """Parsed sessions as ``[(key, header_lines, body_lines), ...]``, oldest first.

        Sessions with no banner and no actual output are dropped: the scraper
        writes a blank line before each banner, which would otherwise produce an
        empty "Earlier output" tab in a freshly created log.
        """
        out = []
        for i, (header, body) in enumerate(self._split_sessions(self._raw_log_lines)):
            if not header and not any(line.strip() for line in body):
                continue
            out.append((self._session_key(header, i), header, body))
        return out

    def _render_console(self):
        """Refresh the session list and show the selected session's log text.

        Only the selected session is rendered. That keeps the pane a plain text
        widget - so multi-line selection and Ctrl+C work - instead of needing an
        in-place collapsible tree.
        """
        sessions = self._console_sessions()
        filter_term = self._console_filter_edit.text().strip().lower()

        self._console_session_tabs.set_sessions(
            [(key, self._session_title(header)) for key, header, _b in sessions]
        )

        if not sessions:
            self._console_text.setPlainText("")
            return

        by_key = {key: (header, body) for key, header, body in sessions}
        current = self._console_session_tabs.current_key()

        def matches(body):
            return not filter_term or any(
                filter_term in line.lower() for line in body
            )

        # A filter can leave the current session empty; jump to the newest one
        # that actually has matches rather than showing a blank pane. Without a
        # filter the user's selection is always honoured.
        if filter_term and (current not in by_key or not matches(by_key[current][1])):
            fallback = next(
                (key for key, _h, body in reversed(sessions) if matches(body)),
                current,
            )
            if fallback in by_key:
                self._console_session_tabs.set_current_key(fallback)
                current = fallback

        header, body = by_key[current]
        shown = [
            line for line in body
            if not filter_term or filter_term in line.lower()
        ]
        out = [line.rstrip("\r\n") for line in header]
        out.extend(line.rstrip("\r\n") for line in shown if line.strip())
        self._console_text.setPlainText("\n".join(out))
        # Only auto-scroll if user is following the live (newest) session
        if self._console_autoscroll_cb.isChecked() and self._console_session_tabs.is_following_live():
            self._scroll_to_bottom()

    def _console_visible_text(self) -> str:
        """Currently rendered log text (used by tests and Clear)."""
        return self._console_text.toPlainText()

    def _append_log_text(self, text: str):
        was_following_live = self._console_session_tabs.is_following_live()
        self._raw_log_lines.extend(text.splitlines(True))
        if len(self._raw_log_lines) > 10000:
            self._raw_log_lines = self._raw_log_lines[-10000:]

        if "[Browser Resolve]" in text or "Opening browser" in text:
            self._on_browser_monitor_tick()

        self._render_console()
        # Follow a brand-new session only if the user was already tailing the
        # newest one; never yank them away from a session they are reading.
        if was_following_live:
            self._console_session_tabs.set_current_key(
                self._console_session_tabs._keys[0] if self._console_session_tabs._keys else ""
            )
            self._render_console()

    def _on_console_session_selected(self, key: str):
        self._render_console()

    def _on_console_filter_changed(self, text: str):
        self._render_console()

    def _on_console_wrap_toggled(self, checked: bool):
        mode = QPlainTextEdit.LineWrapMode.WidgetWidth if checked else QPlainTextEdit.LineWrapMode.NoWrap
        self._console_text.setLineWrapMode(mode)

    def _on_clear_console_clicked(self):
        self._console_text.clear()
        self._raw_log_lines.clear()
        self._console_session_tabs.set_sessions([])

    def _on_open_console_file_clicked(self):
        from my_idm.external_tools import open_file_in_default_app
        log_path = self._manager.external_tools_config.get_console_log_path()
        open_file_in_default_app(log_path, create_if_missing=True)

    def _on_toggle_scraper_clicked(self):
        if self._manager.is_animepahe_running():
            self._manager.stop_animepahe_scraper()
        else:
            self._manager.start_animepahe_scraper()

    def _on_open_animepahe_gui_clicked(self):
        cfg = self._manager.external_tools_config
        repo = cfg.get_effective_repo_path()
        if not repo or not os.path.isdir(repo):
            win = self.window()
            if win is not None and hasattr(win, "_on_open_external_tools_settings"):
                res = QMessageBox.question(
                    self,
                    "AnimePahe Not Configured",
                    "The AnimePahe repository folder is not configured or does not exist.\n\n"
                    "Would you like to configure the repository location in Preferences now?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if res == QMessageBox.StandardButton.Yes:
                    win._on_open_external_tools_settings()
                return

            QMessageBox.warning(
                self,
                "AnimePahe Not Configured",
                "The AnimePahe repository folder is not configured or does not exist.\n\n"
                "Please configure the repository location in Preferences.",
            )
            return

        ok, msg = launch_animepahe_gui(cfg)
        if ok:
            self._show_status_message("Launched AnimePahe Downloader GUI")
        else:
            QMessageBox.warning(self, "Failed to Launch AnimePahe GUI", msg)

    def _scroll_to_bottom(self):
        sb = self._console_text.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _update_console_header(self, is_running: Optional[bool] = None):
        self._lbl_icon.setText("🎬")
        self._lbl_title.setText("AnimePahe CLI Scraper Console")
        if is_running is None:
            is_running = self._manager.is_animepahe_running()
        self._lbl_badge.setText("ACTIVE" if is_running else "STOPPED")
        self._lbl_badge.setStyleSheet(
            f"background-color: {'#193524' if is_running else '#351919'}; "
            f"color: {'#50fa7b' if is_running else '#ff5555'}; "
            f"padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 11px;"
        )
        self._lbl_badge.setVisible(True)

        repo = self._manager.external_tools_config.get_effective_repo_path()
        self._btn_open_folder.setText("📁 Open Repo Folder")
        self._btn_open_folder.setVisible(bool(repo and Path(repo).exists()))

    def show_animepahe_console(self):
        """Switch to Console mode, update header, and start streaming."""
        self.set_mode("console")
        self._update_console_header()
        self._start_log_timer()

    def show_console(self):
        """Alias for show_animepahe_console."""
        self.show_animepahe_console()

    def show_details(self):
        """Switch to Details mode, update header, and stop log timer."""
        self.set_mode("details")

    def is_animepahe_console_active(self) -> bool:
        """Returns True if the panel is currently in Console mode."""
        return self.current_mode() == "console"

    @property
    def browser_container_hwnd(self) -> Optional[int]:
        """HWND of the embedded browser container widget."""
        if hasattr(self, "_browser_container"):
            try:
                return self._browser_container.hwnd()
            except Exception:
                return 0x1234 if os.environ.get("CI") == "true" else None
        return None

    def is_browser_tab_active(self) -> bool:
        """Returns True if the Embedded Browser subtab is currently shown and selected."""
        if not hasattr(self, "_console_subtabs") or not hasattr(self, "_browser_tab"):
            return False
        return (
            self._console_subtabs.indexOf(self._browser_tab) >= 0
            and self._console_subtabs.currentWidget() == self._browser_tab
        )

    def is_browser_attached(self) -> bool:
        """Returns True if an external browser window is currently docked in the container."""
        if not hasattr(self, "_browser_container"):
            return False
        return self._browser_container.is_attached()

    def show_browser_tab(self, chrome_hwnd: Optional[int] = None):
        """Unhides the Browser subtab, attaches the browser HWND if provided, and switches to it."""
        if not hasattr(self, "_console_subtabs") or not hasattr(self, "_browser_tab"):
            return

        if self._console_subtabs.indexOf(self._browser_tab) == -1:
            self._console_subtabs.addTab(self._browser_tab, "🌐 Embedded Browser")

        self._browser_tab.show()
        self._browser_container.show()

        if chrome_hwnd:
            self._browser_container.attach_window(chrome_hwnd)
            self._browser_status_lbl.setText("● Active")
            self._browser_status_lbl.setStyleSheet("color: #50fa7b; font-weight: bold; font-size: 11px;")

        self._console_subtabs.setCurrentWidget(self._browser_tab)
        self.set_mode("console")
        self.browser_tab_requested.emit()

    def hide_browser_tab(self):
        """Detaches browser window, removes Browser subtab, and switches back to Log subtab."""
        if not hasattr(self, "_console_subtabs") or not hasattr(self, "_browser_tab"):
            return

        self._browser_container.detach_window()
        self._browser_tab.hide()
        self._browser_container.hide()
        self._is_browser_floating = False
        if hasattr(self, "_btn_float_browser"):
            self._btn_float_browser.setText("↗ Detach Window")

        idx = self._console_subtabs.indexOf(self._browser_tab)
        if idx >= 0:
            self._console_subtabs.removeTab(idx)

        self._console_subtabs.setCurrentWidget(self._console_log_tab)
        self._browser_status_lbl.setText("○ Idle")
        self._browser_status_lbl.setStyleSheet("color: #ff5555; font-weight: bold; font-size: 11px;")

    def _on_browser_monitor_tick(self):
        """Periodic check for newly opened AnimePahe Chrome windows or closed sessions."""
        if not hasattr(self._manager, "is_animepahe_running") or not self._manager.is_animepahe_running():
            if self.is_browser_attached():
                self.hide_browser_tab()
            return

        proc = getattr(self._manager, "animepahe_process", None)
        if not proc or not getattr(proc, "pid", None):
            return

        if not self.is_browser_attached():
            chrome_hwnd = find_chrome_hwnd(proc.pid)
            if chrome_hwnd:
                # Prevent double-docking race: check if Chrome window is already
                # a child of our container (CLI may have already reparented it)
                if sys.platform == "win32":
                    try:
                        import ctypes
                        parent = ctypes.windll.user32.GetParent(ctypes.c_void_p(chrome_hwnd))
                        if parent == self._browser_container.hwnd():
                            # Already attached by CLI, just show the tab
                            self.show_browser_tab(chrome_hwnd)
                        else:
                            self.show_browser_tab(chrome_hwnd)
                    except Exception:
                        self.show_browser_tab(chrome_hwnd)
                else:
                    self.show_browser_tab(chrome_hwnd)
        else:
            hwnd = self._browser_container.chrome_hwnd
            if hwnd and sys.platform == "win32":
                try:
                    import ctypes
                    if not ctypes.windll.user32.IsWindow(hwnd):
                        self.hide_browser_tab()
                except Exception:
                    pass

    def on_animepahe_status_changed(self, is_running: bool):
        """Slot called whenever the AnimePahe CLI scraper starts or stops."""
        if hasattr(self, "_console_status_lbl"):
            self._console_status_lbl.setText("● Active" if is_running else "○ Stopped")
            self._console_status_lbl.setStyleSheet(
                "color: #50fa7b; font-weight: bold; font-size: 11px;"
                if is_running else
                "color: #ff5555; font-weight: bold; font-size: 11px;"
            )
        if hasattr(self, "_console_action_btn"):
            self._console_action_btn.setText("⏹️ Stop Scraper" if is_running else "▶️ Start Scraper")

        proc = getattr(self._manager, "_animepahe_process", None)
        if hasattr(self, "_console_info_lbl"):
            if is_running and proc and hasattr(proc, "pid"):
                info = f"PID: {proc.pid}"
                if not embedded_browser_supported():
                    # Say why the Embedded Browser subtab is not coming, rather than leaving the
                    # user to wonder why launching the scraper opened a browser they cannot see
                    # embedded here. The scraper itself works fine - only the in-panel view is
                    # unavailable, because reparenting a foreign top-level window into a Qt tab
                    # has no portable equivalent.
                    info += "  ·  Embedded Browser is Windows-only; the scraper's own window opens separately"
                self._console_info_lbl.setText(info)
            else:
                self._console_info_lbl.setText("")

        if is_running:
            self._browser_monitor_timer.start()
        else:
            self._browser_monitor_timer.stop()
            if self.is_browser_attached():
                self.hide_browser_tab()

        if self.current_mode() == "console":
            self._update_console_header(is_running)
            if is_running and self.isVisible():
                self._start_log_timer()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._stop_log_timer()

    def showEvent(self, event):
        super().showEvent(event)
        if self.current_mode() == "console":
            self._start_log_timer()

    # -- State Persistence ----------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Return serializable state of the details panel (active mode, active tab index, etc.)."""
        return {
            "current_mode": self.current_mode(),
            "current_tab": self._tabs.currentIndex(),
        }

    def restore_state(self, state: dict[str, Any]):
        """Restore serializable state of the details panel."""
        if not isinstance(state, dict):
            return
        mode = state.get("current_mode")
        if mode in ("details", "console"):
            self.set_mode(mode)
        tab_idx = state.get("current_tab")
        if tab_idx is not None:
            try:
                idx = int(tab_idx)
                if 0 <= idx < self._tabs.count():
                    self._tabs.setCurrentIndex(idx)
            except (ValueError, TypeError):
                pass
