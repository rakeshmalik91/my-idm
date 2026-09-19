"""Main application window for My-IDM."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStatusBar,
    QTableView,
    QToolBar,
    QWidget,
)

from my_idm.database import Database, DownloadEntry
from my_idm.delegates import ProgressBarDelegate
from my_idm.dialogs import AddDownloadDialog, DeleteConfirmDialog, MoveDownloadDialog
from my_idm.download_model import Col, DownloadTableModel
from my_idm.manager import DownloadManager
from my_idm.styles import Colors

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """The main My-IDM window."""

    def __init__(self, manager: DownloadManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._manager = manager

        self.setWindowTitle("My-IDM — Download Manager")
        self.setMinimumSize(1100, 600)
        self.resize(1400, 750)

        # Model
        self._model = DownloadTableModel(self)

        self._setup_ui()
        self._setup_actions()
        self._setup_toolbar()
        self._setup_menubar()
        self._setup_statusbar()
        self._connect_signals()

        # Load existing downloads from DB
        self._load_history()

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
        self._table.setSortingEnabled(False)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)

        # Progress bar delegate
        self._progress_delegate = ProgressBarDelegate(self._table)
        self._table.setItemDelegateForColumn(
            Col.PROGRESS, self._progress_delegate
        )

        # Column sizing
        header = self._table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(
            Col.NAME, QHeaderView.ResizeMode.Stretch
        )
        header.setDefaultSectionSize(110)

        # Set specific column widths
        self._table.setColumnWidth(Col.SIZE, 90)
        self._table.setColumnWidth(Col.PROGRESS, 160)
        self._table.setColumnWidth(Col.STATUS, 110)
        self._table.setColumnWidth(Col.SPEED, 110)
        self._table.setColumnWidth(Col.ETA, 80)
        self._table.setColumnWidth(Col.TYPE, 65)
        self._table.setColumnWidth(Col.SEEDS_PEERS, 100)
        self._table.setColumnWidth(Col.ADDED, 130)
        self._table.setColumnWidth(Col.LAST_TRIED, 130)
        self._table.setColumnWidth(Col.COMPLETED, 130)

        # Row height
        self._table.verticalHeader().setDefaultSectionSize(36)

        # Context menu
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._table.customContextMenuRequested.connect(self._show_context_menu)

        self.setCentralWidget(self._table)

    def _setup_actions(self):
        """Create all QActions."""
        self._act_add = QAction("➕ Add URL", self)
        self._act_add.setShortcut(QKeySequence("Ctrl+N"))
        self._act_add.setToolTip("Add HTTP URL or Magnet Link (Ctrl+N)")
        self._act_add.triggered.connect(self._on_add)

        self._act_add_torrent = QAction("📦 Add Torrent", self)
        self._act_add_torrent.setShortcut(QKeySequence("Ctrl+T"))
        self._act_add_torrent.setToolTip("Add .torrent file (Ctrl+T)")
        self._act_add_torrent.triggered.connect(self._on_add_torrent)

        self._act_pause = QAction("⏸ Pause", self)
        self._act_pause.setShortcut(QKeySequence("Space"))
        self._act_pause.setToolTip("Pause selected downloads")
        self._act_pause.triggered.connect(self._on_pause)

        self._act_resume = QAction("▶ Resume", self)
        self._act_resume.setShortcut(QKeySequence("Ctrl+R"))
        self._act_resume.setToolTip("Resume selected downloads (Ctrl+R)")
        self._act_resume.triggered.connect(self._on_resume)

        self._act_delete = QAction("🗑 Delete", self)
        self._act_delete.setShortcut(QKeySequence("Delete"))
        self._act_delete.setToolTip("Delete selected downloads")
        self._act_delete.triggered.connect(self._on_delete)

        self._act_move = QAction("📂 Move", self)
        self._act_move.setToolTip("Move download to another directory")
        self._act_move.triggered.connect(self._on_move)

        self._act_recheck = QAction("🔄 Recheck", self)
        self._act_recheck.setToolTip("Verify existing files on disk")
        self._act_recheck.triggered.connect(self._on_recheck)

        self._act_open_file = QAction("📄 Open File", self)
        self._act_open_file.setShortcut(QKeySequence("Return"))
        self._act_open_file.setToolTip("Open the downloaded file")
        self._act_open_file.triggered.connect(self._on_open_file)

        self._act_open_folder = QAction("📁 Open Folder", self)
        self._act_open_folder.setShortcut(QKeySequence("Ctrl+O"))
        self._act_open_folder.setToolTip("Open containing folder (Ctrl+O)")
        self._act_open_folder.triggered.connect(self._on_open_folder)

        self._act_load_backlog = QAction("📋 Load Backlog", self)
        self._act_load_backlog.setShortcut(QKeySequence("Ctrl+L"))
        self._act_load_backlog.setToolTip("Load URLs from a backlog file")
        self._act_load_backlog.triggered.connect(self._on_load_backlog)

    def _setup_toolbar(self):
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        toolbar.addAction(self._act_add)
        toolbar.addAction(self._act_add_torrent)
        toolbar.addSeparator()
        toolbar.addAction(self._act_pause)
        toolbar.addAction(self._act_resume)
        toolbar.addSeparator()
        toolbar.addAction(self._act_delete)
        toolbar.addAction(self._act_move)
        toolbar.addAction(self._act_recheck)
        toolbar.addSeparator()
        toolbar.addAction(self._act_open_file)
        toolbar.addAction(self._act_open_folder)

        self.addToolBar(toolbar)

    def _setup_menubar(self):
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(self._act_add)
        file_menu.addAction(self._act_add_torrent)
        file_menu.addAction(self._act_load_backlog)
        file_menu.addSeparator()

        exit_act = QAction("Exit", self)
        exit_act.setShortcut(QKeySequence("Ctrl+Q"))
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        # Edit menu
        edit_menu = menubar.addMenu("&Edit")
        edit_menu.addAction(self._act_pause)
        edit_menu.addAction(self._act_resume)
        edit_menu.addSeparator()
        edit_menu.addAction(self._act_delete)
        edit_menu.addAction(self._act_move)
        edit_menu.addAction(self._act_recheck)

        # View menu
        view_menu = menubar.addMenu("&View")
        select_all_act = QAction("Select All", self)
        select_all_act.setShortcut(QKeySequence("Ctrl+A"))
        select_all_act.triggered.connect(self._table.selectAll)
        view_menu.addAction(select_all_act)

        # Help menu
        help_menu = menubar.addMenu("&Help")
        about_act = QAction("About My-IDM", self)
        about_act.triggered.connect(self._on_about)
        help_menu.addAction(about_act)

    def _setup_statusbar(self):
        self._status_label = QLabel("Ready")
        self._speed_label = QLabel("")
        self._count_label = QLabel("0 downloads")

        status_bar = QStatusBar()
        status_bar.addWidget(self._status_label, 1)
        status_bar.addPermanentWidget(self._speed_label)
        status_bar.addPermanentWidget(self._count_label)
        self.setStatusBar(status_bar)

    def _connect_signals(self):
        self._manager.progress_updated.connect(self._on_progress_updated)
        self._manager.status_changed.connect(self._on_status_changed)
        self._manager.download_added.connect(self._on_download_added)
        self._manager.download_removed.connect(self._on_download_removed)
        self._manager.download_moved.connect(self._on_download_moved)

    # -- data loading --------------------------------------------------------

    def _load_history(self):
        entries = self._manager.get_all_entries()
        self._model.load_entries(entries)
        self._update_count_label()

    # -- selected entries helper ---------------------------------------------

    def _selected_ids(self) -> list[str]:
        return self._model.get_selected_ids(
            self._table.selectionModel().selectedIndexes()
        )

    def _first_selected_entry(self) -> Optional[DownloadEntry]:
        ids = self._selected_ids()
        if ids:
            return self._model.get_entry_by_id(ids[0])
        return None

    # -- action handlers -----------------------------------------------------

    def _on_add(self):
        dlg = AddDownloadDialog(self)
        if dlg.exec() == AddDownloadDialog.DialogCode.Accepted:
            self._manager.add_download(
                dlg.url, dlg.save_path, dlg.num_segments
            )

    def _on_add_torrent(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Torrent Files", "",
            "Torrent Files (*.torrent);;All Files (*)",
        )
        for path in paths:
            self._manager.add_download(path)

    def _on_pause(self):
        for did in self._selected_ids():
            self._manager.pause_download(did)

    def _on_resume(self):
        for did in self._selected_ids():
            self._manager.resume_download(did)

    def _on_delete(self):
        ids = self._selected_ids()
        if not ids:
            return
        dlg = DeleteConfirmDialog(len(ids), self)
        if dlg.exec() == DeleteConfirmDialog.DialogCode.Accepted:
            for did in ids:
                self._manager.delete_download(did, dlg.delete_files)

    def _on_move(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        dlg = MoveDownloadDialog(entry.save_path, self)
        if dlg.exec() == MoveDownloadDialog.DialogCode.Accepted:
            for did in self._selected_ids():
                self._manager.move_download(did, dlg.new_path)

    def _on_recheck(self):
        for did in self._selected_ids():
            self._manager.recheck_download(did)

    def _on_open_file(self):
        entry = self._first_selected_entry()
        if entry and entry.file_path and Path(entry.file_path).exists():
            os.startfile(entry.file_path)

    def _on_open_folder(self):
        entry = self._first_selected_entry()
        if not entry:
            return
        folder = entry.save_path
        file_path = entry.file_path
        if file_path and Path(file_path).exists():
            # Open explorer with file selected
            if sys.platform == "win32":
                subprocess.Popen(
                    ["explorer", "/select,", file_path.replace("/", "\\")]
                )
            else:
                os.startfile(folder)
        elif folder and Path(folder).exists():
            os.startfile(folder)

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

    def _on_about(self):
        QMessageBox.about(
            self, "About My-IDM",
            "<h2>My-IDM v1.0.0</h2>"
            "<p>A full-featured download manager with segmented HTTP "
            "downloads, torrent support, pause/resume, and more.</p>"
            "<p>Built with PySide6, aiohttp, and libtorrent.</p>",
        )

    # -- context menu --------------------------------------------------------

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction(self._act_pause)
        menu.addAction(self._act_resume)
        menu.addSeparator()
        menu.addAction(self._act_recheck)
        menu.addAction(self._act_move)
        menu.addSeparator()
        menu.addAction(self._act_open_file)
        menu.addAction(self._act_open_folder)
        menu.addSeparator()
        menu.addAction(self._act_delete)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    # -- signal handlers from manager ----------------------------------------

    def _on_progress_updated(self, download_id: str, downloaded: int,
                             total: int, speed: float, eta: float,
                             seeds: int, peers: int, upload_speed: float):
        self._model.update_progress(
            download_id, downloaded, total, speed, eta,
            seeds, peers, upload_speed,
        )

    def _on_status_changed(self, download_id: str, status: str,
                           error_msg: str):
        self._model.update_status(download_id, status, error_msg)
        self._update_count_label()

    def _on_download_added(self, download_id: str):
        entry = self._manager.get_entry(download_id)
        if entry:
            self._model.add_entry(entry)
            self._update_count_label()

    def _on_download_removed(self, download_id: str):
        self._model.remove_entry(download_id)
        self._update_count_label()

    def _on_download_moved(self, download_id: str):
        entry = self._manager.get_entry(download_id)
        if entry:
            self._model.refresh_entry(download_id, entry)

    # -- helpers -------------------------------------------------------------

    def _update_count_label(self):
        total = self._model.rowCount()
        self._count_label.setText(f"{total} download(s)")

    def closeEvent(self, event):
        self._manager.stop()
        super().closeEvent(event)
