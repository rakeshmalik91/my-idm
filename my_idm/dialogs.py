"""Dialog windows for My-IDM."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from my_idm.clipboard_monitor import looks_like_download_url
from my_idm.config import GeneralConfig, DEFAULT_DOWNLOADS_DIR, TorConfig
from my_idm.database import DEFAULT_QUEUE_COLOR, normalize_queue_color
from my_idm.youtube_tool import detect_youtube_url

DEFAULT_SAVE_PATH = DEFAULT_DOWNLOADS_DIR


class AddDownloadDialog(QDialog):
    """Dialog to add a new download (URL, magnet link, YouTube URL, or .torrent file)."""

    def __init__(self, parent=None, initial_url: str = "", manager=None):
        super().__init__(parent)
        self._manager = manager
        if self._manager is None and parent and hasattr(parent, "_manager"):
            self._manager = parent._manager

        self.setWindowTitle("Add Download")
        self.setMinimumWidth(550)
        self.setModal(True)

        from my_idm.resources import get_app_icon
        self.setWindowIcon(get_app_icon())

        self._config = GeneralConfig.load()
        self._url = ""
        self._urls: list[str] = []
        self._yt_result: dict = {}
        self._save_path = self._config.get_effective_save_path()
        self._num_segments = self._config.default_segments
        self._tor_enabled = (
            bool(self._manager.tor_config.enabled)
            if (self._manager and hasattr(self._manager, "tor_config"))
            else bool(TorConfig.load().enabled)
        )

        self._setup_ui()
        self._prefill_url(initial_url)

        if self._manager and hasattr(self._manager, "tor_config_changed"):
            def _on_tor_changed(cfg):
                self._tor_enabled = bool(cfg.enabled)
                self._update_tor_btn()
            self._manager.tor_config_changed.connect(_on_tor_changed)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # URL / Magnet input
        url_group = QGroupBox("URL / Magnet Link / YouTube URL")
        url_layout = QVBoxLayout(url_group)

        self._url_edit = QPlainTextEdit()
        self._url_edit.text = self._url_edit.toPlainText
        self._url_edit.setText = self._url_edit.setPlainText
        self._url_edit.hasSelectedText = lambda: self._url_edit.textCursor().hasSelection()
        self._url_edit.setPlaceholderText(
            "Paste URL(s), magnet link(s), one per line, or browse for .torrent file(s)..."
        )
        self._url_edit.setFixedHeight(90)
        url_layout.addWidget(self._url_edit)

        browse_layout = QHBoxLayout()
        browse_btn = QPushButton("Browse .torrent …")
        browse_btn.clicked.connect(self._browse_torrent)
        browse_layout.addStretch()
        browse_layout.addWidget(browse_btn)
        url_layout.addLayout(browse_layout)

        self._yt_banner = QFrame()
        self._yt_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self._yt_banner.setStyleSheet(
            "QFrame { background:#12261e; border:1px solid #2ea043; border-radius:4px; }"
        )
        yt_layout = QHBoxLayout(self._yt_banner)
        yt_layout.setContentsMargins(10, 8, 10, 8)
        self._yt_banner_label = QLabel("This looks like a YouTube link.")
        self._yt_banner_label.setStyleSheet("color:#7ee787; font-weight:bold;")
        yt_layout.addWidget(self._yt_banner_label, 1)
        self._yt_open_btn = QPushButton("Open YouTube Downloader")
        self._yt_open_btn.setObjectName("primaryButton")
        self._yt_open_btn.setToolTip(
            "Analyse the video and choose a quality. yt-dlp is required."
        )
        self._yt_open_btn.clicked.connect(self._on_open_youtube_dialog)
        yt_layout.addWidget(self._yt_open_btn)
        self._yt_banner.setVisible(False)
        url_layout.addWidget(self._yt_banner)

        layout.addWidget(url_group)

        # Save location
        save_group = QGroupBox("Save Location")
        save_layout = QVBoxLayout(save_group)
        save_layout.setSpacing(6)

        path_row = QHBoxLayout()
        db = self._manager._db if self._manager and hasattr(self._manager, "_db") else None
        recent_folders = db.get_recent_save_paths(5) if db else []
        self._save_edit = QComboBox()
        self._save_edit.setEditable(True)
        self._save_edit.setInsertPolicy(QComboBox.NoInsert)
        self._save_edit.setEditText(self._save_path)
        seen: set[str] = set()
        all_paths: list[str] = []
        for folder in [self._save_path] + recent_folders:
            norm = folder.lower() if folder else ""
            if norm and norm not in seen:
                seen.add(norm)
                all_paths.append(folder)
        for folder in all_paths:
            self._save_edit.addItem(folder)
        path_row.addWidget(self._save_edit, 1)

        save_browse_btn = QPushButton("Browse …")
        save_browse_btn.clicked.connect(self._browse_save_path)
        path_row.addWidget(save_browse_btn)
        save_layout.addLayout(path_row)

        self._set_as_default_cb = QCheckBox("Set as default download folder")
        save_layout.addWidget(self._set_as_default_cb)

        layout.addWidget(save_group)

        # Options
        options_group = QGroupBox("Options")
        options_layout = QHBoxLayout(options_group)

        options_layout.addWidget(QLabel("Segments:"))
        self._seg_spin = QSpinBox()
        self._seg_spin.setRange(1, 32)
        self._seg_spin.setValue(self._num_segments)
        self._seg_spin.setToolTip(
            "Number of parallel connections for HTTP downloads"
        )
        options_layout.addWidget(self._seg_spin)
        options_layout.addStretch()

        layout.addWidget(options_group)

        # Buttons
        btn_layout = QHBoxLayout()

        self._tor_btn = QPushButton("🧅 Tor: OFF")
        self._tor_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tor_btn.clicked.connect(self._on_toggle_tor)
        self._update_tor_btn()
        btn_layout.addWidget(self._tor_btn)

        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("Download")
        ok_btn.setObjectName("primaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

    def is_tor_enabled(self) -> bool:
        return bool(self._tor_enabled)

    def _update_tor_btn(self):
        enabled = self.is_tor_enabled()
        if enabled:
            self._tor_btn.setText("🧅 Tor: ON")
            self._tor_btn.setToolTip("Tor network privacy is active. Click to toggle OFF.")
            self._tor_btn.setStyleSheet("""
                QPushButton {
                    background-color: #1b472c;
                    color: #50fa7b;
                    border: 1px solid #50fa7b;
                    border-radius: 4px;
                    padding: 5px 12px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #235e3a;
                }
            """)
        else:
            self._tor_btn.setText("🧅 Tor: OFF")
            self._tor_btn.setToolTip("Tor network privacy is inactive. Click to toggle ON.")
            self._tor_btn.setStyleSheet("""
                QPushButton {
                    background-color: #21262d;
                    color: #8b949e;
                    border: 1px solid #30363d;
                    border-radius: 4px;
                    padding: 5px 12px;
                    font-weight: 500;
                }
                QPushButton:hover {
                    background-color: #30363d;
                    color: #c9d1d9;
                }
            """)

    def _on_toggle_tor(self):
        target = not self._tor_enabled
        if self._manager and hasattr(self._manager, "toggle_tor"):
            success, msg = self._manager.toggle_tor(target)
            if not success and target:
                QMessageBox.critical(
                    self,
                    "⚠️ Tor Connection Error",
                    f"Unable to activate Tor network privacy:\n\n{msg}\n\n"
                    "Please verify that Tor or Tor Browser is installed, or configure the path in Tools → Tor Network Settings.",
                )
                self._tor_enabled = False
            else:
                self._tor_enabled = target
        else:
            self._tor_enabled = target
        self._update_tor_btn()

    def _browse_torrent(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Torrent File(s)", "",
            "Torrent Files (*.torrent);;All Files (*)",
        )
        if paths:
            existing = self._url_edit.toPlainText().strip()
            lines = [l.strip() for l in existing.splitlines() if l.strip()] if existing else []
            for p in paths:
                if p not in lines:
                    lines.append(p)
            self._url_edit.setPlainText("\n".join(lines))

    def _browse_save_path(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Save Directory", self._save_edit.currentText()
        )
        if path:
            self._save_edit.setEditText(path)

    @staticmethod
    def _is_valid_download_url(text: str) -> bool:
        # Shared with clipboard capture so the pre-fill gate and the auto-capture gate cannot
        # drift: the same string must be offered here and captured there, or neither.
        return looks_like_download_url(text)

    def _prefill_url(self, initial_url: str = ""):
        candidates: list[str] = []
        if initial_url:
            for line in initial_url.splitlines():
                if line.strip():
                    candidates.append(line.strip())
        else:
            clipboard = QGuiApplication.clipboard()
            if clipboard:
                text = (clipboard.text() or "").strip()
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                if lines and all(self._is_valid_download_url(l) for l in lines):
                    candidates = lines

        if candidates:
            self._url_edit.setPlainText("\n".join(candidates))
            self._url_edit.selectAll()

        self._update_youtube_banner()
        self._url_edit.textChanged.connect(self._update_youtube_banner)

    # -- YouTube detection / redirect ----------------------------------------

    def _detected_youtube_url(self) -> str:
        """Return the YouTube URL currently in the input box, or an empty string."""
        if not self._youtube_detection_enabled():
            return ""
        text = self._url_edit.toPlainText() if hasattr(self, "_url_edit") else ""
        if not text.strip():
            return ""
        return detect_youtube_url(text) or ""

    def _youtube_detection_enabled(self) -> bool:
        if self._manager is not None and hasattr(self._manager, "external_tools_config"):
            return bool(self._manager.external_tools_config.ytdlp_auto_detect_urls)
        try:
            from my_idm.config import ExternalToolsConfig
            return bool(ExternalToolsConfig.load().ytdlp_auto_detect_urls)
        except Exception:
            return False

    def _update_youtube_banner(self):
        """Show or hide the YouTube hand-off banner based on the pasted text."""
        banner = getattr(self, "_yt_banner", None)
        if banner is None:
            return
        url = self._detected_youtube_url()
        banner.setVisible(bool(url))
        if url:
            self._yt_banner_label.setText("YouTube link detected — open the YouTube downloader to pick a quality.")

    @property
    def youtube_url(self) -> str:
        """The detected YouTube URL, if any."""
        return self._detected_youtube_url()

    def _on_open_youtube_dialog(self):
        """Hand the pasted YouTube URL to the YouTube download dialog.

        Dismissing the YouTube dialog leaves this one open with the hand-off
        banner still showing, so the link can be re-opened without re-pasting.
        """
        url = self._detected_youtube_url()
        if not url:
            return
        from my_idm.youtube_dialog import YouTubeDialog

        dlg = YouTubeDialog(self, manager=self._manager, initial_url=url)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._yt_result = dlg.selection()
            self.accept()
        else:
            self._update_youtube_banner()

    @property
    def youtube_selection(self) -> dict:
        """Selection returned by the YouTube dialog, if it was used."""
        return getattr(self, "_yt_result", {}) or {}

    def _accept(self):
        raw_text = self._url_edit.toPlainText().strip()
        self._urls = [l.strip() for l in raw_text.splitlines() if l.strip()]
        self._url = self._urls[0] if self._urls else ""
        self._save_path = self._save_edit.currentText().strip()
        self._num_segments = self._seg_spin.value()
        if self._urls:
            if self._save_path:
                if self._set_as_default_cb.isChecked():
                    self._config.default_save_path = self._save_path
                    self._config.last_save_path = self._save_path
                elif self._config.remember_last_save_path:
                    self._config.last_save_path = self._save_path
                self._config.save()
            self.accept()

    @property
    def url(self) -> str:
        if self._url:
            return self._url
        urls = self.urls
        return urls[0] if urls else ""

    @property
    def urls(self) -> list[str]:
        if getattr(self, "_urls", None):
            return self._urls
        raw = self._url_edit.toPlainText().strip()
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        return lines if lines else ([self._url] if self._url else [])

    @property
    def save_path(self) -> str:
        return self._save_path

    @property
    def num_segments(self) -> int:
        return self._num_segments


class MoveDownloadDialog(QDialog):
    """Dialog to choose a new save location for a download."""

    def __init__(self, current_path: str = "", parent=None, db=None):
        super().__init__(parent)
        self.setWindowTitle("Move Download")
        self.setMinimumWidth(500)
        self.setModal(True)

        self._new_path = ""
        self._db = db

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("Select new save directory:"))

        path_layout = QHBoxLayout()
        recent_folders = self._db.get_recent_save_paths(5) if self._db else []
        default_path = GeneralConfig.load().get_effective_save_path()
        self._path_edit = QComboBox()
        self._path_edit.setEditable(True)
        self._path_edit.setInsertPolicy(QComboBox.NoInsert)
        self._path_edit.setEditText(current_path)
        seen: set[str] = set()
        all_paths: list[str] = []
        for folder in [current_path, default_path] + recent_folders:
            norm = folder.lower() if folder else ""
            if norm and norm not in seen:
                seen.add(norm)
                all_paths.append(folder)
        for folder in all_paths:
            self._path_edit.addItem(folder)
        path_layout.addWidget(self._path_edit, 1)

        browse_btn = QPushButton("Browse …")
        browse_btn.clicked.connect(self._browse)
        path_layout.addWidget(browse_btn)

        layout.addLayout(path_layout)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("Move")
        ok_btn.setObjectName("primaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Directory", self._path_edit.currentText()
        )
        if path:
            self._path_edit.setEditText(path)

    def _accept(self):
        self._new_path = self._path_edit.currentText().strip()
        if self._new_path:
            self.accept()

    @property
    def new_path(self) -> str:
        return self._new_path


class DeleteConfirmDialog(QDialog):
    """Confirmation dialog for deleting a download."""

    def __init__(self, count: int = 1, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Delete Download")
        self.setMinimumWidth(380)
        self.setModal(True)

        self._delete_files = True

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        items = "this download" if count == 1 else f"these {count} downloads"
        layout.addWidget(QLabel(
            f"Are you sure you want to remove {items}?"
        ))

        self._files_cb = QCheckBox("Also delete downloaded files from disk (move to Trash)")
        self._files_cb.setChecked(True)
        layout.addWidget(self._files_cb)

        layout.addSpacing(8)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.setObjectName("dangerButton")
        delete_btn.setDefault(True)
        delete_btn.clicked.connect(self._accept)
        btn_layout.addWidget(delete_btn)

        layout.addLayout(btn_layout)

    def _accept(self):
        self._delete_files = self._files_cb.isChecked()
        self.accept()

    @property
    def delete_files(self) -> bool:
        return self._delete_files


class RenameDialog(QDialog):
    """Dialog to rename a download's file or root folder name."""

    def __init__(self, current_name: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rename Download")
        self.setMinimumWidth(560)
        self.setModal(True)

        from my_idm.resources import get_app_icon
        self.setWindowIcon(get_app_icon())

        self._new_name = current_name

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("Enter new filename or root folder name:"))

        self._name_edit = QLineEdit(current_name)
        self._name_edit.setClearButtonEnabled(True)
        self._name_edit.returnPressed.connect(self._accept)
        layout.addWidget(self._name_edit)

        # Pre-select basename excluding extension if dot is present
        if "." in current_name and not current_name.startswith("."):
            dot_idx = current_name.rfind(".")
            self._name_edit.setSelection(0, dot_idx)
        else:
            self._name_edit.selectAll()

        layout.addSpacing(8)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("OK")
        ok_btn.setObjectName("primaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

    def _accept(self):
        text = self._name_edit.text().strip()
        if not text:
            QMessageBox.warning(self, "Invalid Name", "Filename cannot be empty.")
            return
        self._new_name = text
        self.accept()

    @property
    def new_name(self) -> str:
        return self._new_name


class QueueManagerDialog(QDialog):
    """Create, rename, reorder, limit and delete named queues.

    The limit is edited in place in the "Max at once" column itself, for every queue including
    Default. A limit of 0 means **Global**: the queue adds no cap of its own and simply follows
    the global concurrency limit. It does *not* mean "unlimited" - the global limit always
    applies on top, which is why the note under the table spells that out.

    Deleting a queue never deletes downloads - the manager moves them to Default and this
    dialog says how many, rather than making the user discover it afterwards.
    """

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Queues")
        self.setMinimumSize(660, 420)
        self._manager = manager
        self._result_message = ""
        # True only while _reload() rebuilds the widgets, so the rebuild cannot itself be read
        # as the user editing a limit.
        self._loading = False

        layout = QVBoxLayout(self)

        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels(
            ["Queue", "Downloads", "Max at once  (0 = Global)"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._table)

        # The note the TODO asked for. Shows the live global value rather than describing it
        # abstractly, because "Global" in the spin box is otherwise a word with no number
        # attached to it.
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color: #8fa0b5;")
        layout.addWidget(self._note)

        btn_row = QHBoxLayout()
        self._up_btn = QPushButton("↑ Move Up")
        self._down_btn = QPushButton("↓ Move Down")
        self._add_btn = QPushButton("Add…")
        self._rename_btn = QPushButton("Rename…")
        self._delete_btn = QPushButton("Delete…")
        self._up_btn.clicked.connect(lambda: self._move_selected(-1))
        self._down_btn.clicked.connect(lambda: self._move_selected(+1))
        self._add_btn.clicked.connect(self._on_add)
        self._rename_btn.clicked.connect(self._on_rename)
        self._delete_btn.clicked.connect(self._on_delete)
        for btn in (self._up_btn, self._down_btn, self._add_btn, self._rename_btn, self._delete_btn):
            btn_row.addWidget(btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._reload()
        self._on_selection_changed()

    def _refresh_note(self):
        """Explain how a queue limit interacts with the global limit, with the live value."""
        global_max = self._manager.general_config.effective_max_concurrent
        self._note.setText(
            "A queue's limit caps how many of its own downloads run at once. "
            "Leave it at 0 for Global: the queue adds no cap of its own and follows the "
            f"global limit, currently {global_max} at a time "
            "(Tools → Preferences → General & Downloads).\n"
            "A download starts only when both its queue's limit and the global limit allow "
            "it, so a queue limit is a ceiling and never a reservation. Default holds every "
            "download that no other queue claims."
        )

    @property
    def result_message(self) -> str:
        return self._result_message

    def _reload(self):
        queues = self._manager.get_queues()
        counts = self._manager._db.get_queue_download_counts()
        selected_id = self._selected_queue_id()

        self._loading = True
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        for queue in queues:
            row = self._table.rowCount()
            self._table.insertRow(row)
            # The colour swatch sits in the same cell as the name, so the queue is
            # identifiable by colour without a column of its own.
            name_item = QTableWidgetItem(queue.name)
            name_item.setData(Qt.ItemDataRole.UserRole, queue.id)
            if queue.is_default:
                name_item.setToolTip(
                    "The default queue. Every download starts here unless another queue "
                    "claims it. Its limit and colour are editable like any other."
                )
            self._table.setItem(row, 0, name_item)
            self._table.setCellWidget(row, 0, self._name_cell(queue))
            self._table.setItem(
                row, 1, QTableWidgetItem(str(counts.get(queue.id, 0)))
            )
            # The editor lives in the "Max at once" column itself. There is no separate
            # edit column: a blank fourth column with a control floating in it read as two
            # unrelated things, and it made the Default queue look un-editable because it was
            # the only row without a control there.
            self._table.setCellWidget(row, 2, self._limit_editor(queue))
        self._table.blockSignals(False)
        self._loading = False

        self._refresh_note()
        if selected_id:
            self._select_queue_id(selected_id)
        self._on_selection_changed()

    def _limit_editor(self, queue):
        spin = QSpinBox(self._table)
        # No setSpecialValueText here, deliberately. It substitutes a word for the number, so
        # typing 0 shows "Global" and typing "Global" is rejected - the field stops agreeing
        # with itself, and "what I typed" is no longer "what I see". The meaning of 0 lives in
        # the column header, the cell tooltip and the note instead, none of which are edited.
        spin.setRange(0, 99)
        spin.setValue(max(0, queue.max_concurrent))
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spin.setToolTip(
            "How many of this queue's downloads may run at once.\n"
            "0 = follow the global limit (no limit of its own)."
        )
        spin.valueChanged.connect(
            lambda value, qid=queue.id: self._on_limit_changed(qid, value)
        )
        return spin

    def _name_cell(self, queue):
        """The swatch plus the queue name, as one cell.

        A cell widget covers its whole cell, so the swatch cannot simply be dropped on top of a
        name item - it would hide it. Hence a small container holding both. The name is *also*
        written to the underlying item, because that is what selection, ``_selected_queue_id``
        and the column-width logic read; the label here is only what gets painted.
        """
        holder = QWidget(self._table)
        row = QHBoxLayout(holder)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(6)

        button = QPushButton(holder)
        button.setFixedSize(16, 16)
        button.setFlat(True)
        colour = normalize_queue_color(queue.color) or DEFAULT_QUEUE_COLOR
        button.setStyleSheet(
            f"QPushButton {{ background-color: {colour}; border: 1px solid #555; "
            f"border-radius: 3px; }}"
            f"QPushButton:hover {{ border: 1px solid #999; }}"
        )
        button.setToolTip(f"Colour for '{queue.name}'. Click to change it.")
        button.clicked.connect(
            lambda _checked=False, qid=queue.id: self._on_pick_color(qid)
        )
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)

        label = QLabel(queue.name, holder)
        label.setToolTip(
            f"'{queue.name}' - click the swatch to change its colour."
        )
        row.addWidget(label, 1)
        return holder

    def _on_pick_color(self, queue_id: str):
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
        self._reload()

    def _selected_queue_id(self) -> str:
        row = self._table.currentRow()
        if row < 0:
            return ""
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _select_queue_id(self, queue_id: str):
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == queue_id:
                self._table.selectRow(row)
                return

    def _on_selection_changed(self):
        queue_id = self._selected_queue_id()
        queue = self._manager.get_queue(queue_id) if queue_id else None
        is_default = bool(queue and queue.is_default)
        self._rename_btn.setEnabled(bool(queue) and not is_default)
        self._delete_btn.setEnabled(bool(queue) and not is_default)
        rows = self._table.rowCount()
        idx = self._table.currentRow()
        # The default queue is pinned first and never moves, so the buttons are disabled when
        # the selection is at either end of the movable run.
        movable = rows - 1 if rows else 0
        position = idx if idx > 0 else 0
        self._up_btn.setEnabled(bool(queue) and not is_default and position > 0)
        self._down_btn.setEnabled(bool(queue) and not is_default and position < movable - 1)

    def _on_limit_changed(self, queue_id: str, value: int):
        if self._loading:
            return
        self._manager.set_queue_max_concurrent(queue_id, value)
        # A limit change can change what the toolbar combo shows, so keep the two in step.
        self._refresh_note()

    def _move_selected(self, delta: int):
        queue_id = self._selected_queue_id()
        if not queue_id:
            return
        self._manager.move_queue_in_list(queue_id, delta)
        self._reload()
        self._select_queue_id(queue_id)

    def _on_add(self):
        name, ok = QInputDialog.getText(
            self, "New Queue", "Queue name:", QLineEdit.Normal, ""
        )
        if not ok:
            return
        created, message = self._manager.create_queue(name.strip(), 3)
        self._result_message = message
        if created:
            self._reload()

    def _on_rename(self):
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
            self._reload()
            self._select_queue_id(queue_id)

    def _on_delete(self):
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
            self._reload()
