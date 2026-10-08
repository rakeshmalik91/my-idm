"""Dialog windows for My-IDM."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication
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
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from my_idm.clipboard_monitor import looks_like_download_url
from my_idm.config import GeneralConfig, DEFAULT_DOWNLOADS_DIR, TorConfig
from my_idm.database import DEFAULT_QUEUE_COLOR, normalize_queue_color
from my_idm.styles import Colors, themed_widget
from my_idm.youtube_tool import detect_animepahe_url, detect_youtube_url

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
        # Set True by _on_open_animepahe_dialog when the scraper was actually
        # started from within Preferences, so the caller can skip the direct
        # URL add that would otherwise treat the pasted animepahe URL as HTTP.
        self.animepahe_handoff = False
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

        # AnimePahe hand-off banner. Mirrors the YouTube banner: a pasted
        # animepahe.* series/episode URL is detected and offered a one-click
        # route into the AnimePahe section of Preferences, with the URL
        # prefilled so the user only has to pick episodes and hit Download.
        self._ap_banner = QFrame()
        self._ap_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self._ap_banner.setStyleSheet(
            "QFrame { background:#26141a; border:1px solid #ff7b72; border-radius:4px; }"
        )
        ap_layout = QHBoxLayout(self._ap_banner)
        ap_layout.setContentsMargins(10, 8, 10, 8)
        self._ap_banner_label = QLabel("This looks like an AnimePahe link.")
        self._ap_banner_label.setStyleSheet("color:#ffa99a; font-weight:bold;")
        ap_layout.addWidget(self._ap_banner_label, 1)
        self._ap_open_btn = QPushButton("Open AnimePahe Downloader")
        self._ap_open_btn.setObjectName("primaryButton")
        self._ap_open_btn.setToolTip(
            "Resolve the series/episodes and forward download jobs to My-IDM."
        )
        self._ap_open_btn.clicked.connect(self._on_open_animepahe_dialog)
        ap_layout.addWidget(self._ap_open_btn)
        self._ap_banner.setVisible(False)
        url_layout.addWidget(self._ap_banner)

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
        self._update_animepahe_banner()
        self._url_edit.textChanged.connect(self._update_youtube_banner)
        self._url_edit.textChanged.connect(self._update_animepahe_banner)

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

    # -- AnimePahe detection / redirect --------------------------------------

    def _detected_animepahe_url(self) -> str:
        """Return the AnimePahe URL currently in the input box, or an empty string."""
        text = self._url_edit.toPlainText() if hasattr(self, "_url_edit") else ""
        if not text.strip():
            return ""
        return detect_animepahe_url(text) or ""

    def _update_animepahe_banner(self):
        """Show or hide the AnimePahe hand-off banner based on the pasted text."""
        banner = getattr(self, "_ap_banner", None)
        if banner is None:
            return
        url = self._detected_animepahe_url()
        banner.setVisible(bool(url))
        if url:
            self._ap_banner_label.setText("AnimePahe link detected — open the downloader to pick episodes.")

    def _on_open_animepahe_dialog(self):
        """Hand the pasted AnimePahe URL to the AnimePahe section of Preferences.

        Mirrors the YouTube hand-off: dismissing the preferences dialog leaves
        this Add Download dialog open with the banner still showing, so the
        link can be re-opened without re-pasting. The URL is prefilled into
        the AnimePahe URL field so the user only picks episodes and hits
        Download.
        """
        url = self._detected_animepahe_url()
        if not url:
            return
        from my_idm.settings_dialog import SettingsDialog, TAB_EXTERNAL_TOOLS

        dlg = SettingsDialog(
            general_config=self._manager.general_config if self._manager else None,
            torrent_config=self._manager.torrent_config if self._manager else None,
            network_config=self._manager.network_config if self._manager else None,
            security_config=self._manager.security_config if self._manager else None,
            tor_config=self._manager.tor_config if self._manager else None,
            external_tools_config=self._manager.external_tools_config if self._manager else None,
            browser_config=self._manager.browser_config if self._manager else None,
            db=self._manager._db if (self._manager and hasattr(self._manager, "_db")) else None,
            parent=self,
            initial_tab=TAB_EXTERNAL_TOOLS,
            manager=self._manager,
        )
        dlg._animepahe_url_edit.setText(url)
        if dlg.exec() == QDialog.DialogCode.Accepted and getattr(dlg, "animepahe_download_started", False):
            # The scraper was started from within Preferences, so the Add
            # Download dialog's job is done — close it rather than leaving the
            # user with two dialogs to dismiss.
            self.animepahe_handoff = True
            self.accept()
        else:
            # User closed Preferences without downloading: keep this dialog
            # open with the banner so the link can be re-opened.
            self._update_animepahe_banner()

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


class RefreshAddressDialog(QDialog):
    """Dialog to update/refresh the source URL of an expired or changed download."""

    def __init__(self, current_url: str = "", filename: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Refresh Download Address")
        self.setMinimumWidth(580)
        self.setModal(True)

        from my_idm.resources import get_app_icon
        self.setWindowIcon(get_app_icon())

        self._new_url = current_url
        self._resume_immediately = True

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        if filename:
            name_lbl = QLabel(f"<b>Download:</b> {filename}")
            layout.addWidget(name_lbl)

        layout.addWidget(QLabel("Current address:"))
        self._current_url_edit = QLineEdit(current_url)
        self._current_url_edit.setReadOnly(True)
        layout.addWidget(self._current_url_edit)

        layout.addWidget(QLabel("New address / URL:"))
        self._url_edit = QLineEdit(current_url)
        self._url_edit.setClearButtonEnabled(True)
        self._url_edit.returnPressed.connect(self._accept)
        layout.addWidget(self._url_edit)
        self._url_edit.selectAll()

        hint_lbl = QLabel(
            "Existing downloaded bytes and completed segments will be preserved when resuming."
        )
        hint_lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        layout.addWidget(hint_lbl)

        self._resume_cb = QCheckBox("Resume download immediately after updating address")
        self._resume_cb.setChecked(True)
        layout.addWidget(self._resume_cb)

        layout.addSpacing(8)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("Update Address")
        ok_btn.setObjectName("primaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

    def _accept(self):
        text = self._url_edit.text().strip()
        if not text:
            QMessageBox.warning(self, "Invalid URL", "Download address cannot be empty.")
            return
        if not (text.startswith("http://") or text.startswith("https://")):
            QMessageBox.warning(
                self,
                "Invalid URL",
                "Please enter a valid HTTP or HTTPS URL (starting with http:// or https://).",
            )
            return
        self._new_url = text
        self._resume_immediately = self._resume_cb.isChecked()
        self.accept()

    @property
    def current_url(self) -> str:
        return self._current_url_edit.text()

    @property
    def new_url(self) -> str:
        return self._new_url

    @property
    def resume_immediately(self) -> bool:
        return self._resume_immediately


class QueueManagerDialog(QDialog):
    """Create, rename, reorder, limit and delete named queues.

    The limits are edited in place in their own columns, for every queue including Default:

    * "Max at once" caps how many of the queue's downloads run simultaneously.
    * "Download limit" and "Upload limit" cap its bandwidth, in KB/s.

    A 0 in any of them means **Global**: the queue adds no cap of its own and simply follows the
    global setting. It does *not* mean "unlimited" - the global limit always applies on top,
    which is why the note under the table spells that out. When both a queue limit and the global
    one are set, the tighter of the two wins, so a queue can lower the global limit but never
    raise it.

    Deleting a queue never deletes downloads - the manager moves them to Default and this
    dialog says how many, rather than making the user discover it afterwards.
    """

    #: Column order. Named because the widths, the editors and `_reload` all index by position
    #: and a bare 0/1/2/3/4 in five places is how the columns drift out of their headers.
    COL_QUEUE, COL_DOWNLOADS, COL_MAX_CONCURRENT, COL_DOWNLOAD_LIMIT, COL_UPLOAD_LIMIT = range(5)

    HEADERS = (
        "Queue",
        "Downloads",
        "Max at once\n(0 = Global)",
        "Download limit\n(0 = Global)",
        "Upload limit\n(0 = Global)",
    )

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Queues")
        # Height only: the width is derived from the headers in _fit_width_to_headers, because a
        # fixed one cannot know what the user's font and DPI need. 460 fits four rows plus the
        # note, the buttons and the dialog buttons without scrolling.
        self.setMinimumHeight(460)
        self.resize(880, 460)
        self._manager = manager
        self._result_message = ""
        # True only while _reload() rebuilds the widgets, so the rebuild cannot itself be read
        # as the user editing a limit.
        self._loading = False

        layout = QVBoxLayout(self)

        self._table = QTableWidget(0, len(self.HEADERS), self)
        self._table.setHorizontalHeaderLabels(list(self.HEADERS))
        # Two lines per header, so the section has to be told to grow: QHeaderView sizes itself
        # for one line and the second would be clipped. Set from the live font rather than
        # guessed, for the same reason the width below is measured.
        _header = self._table.horizontalHeader()
        _line_height = _header.fontMetrics().height()
        _header.setFixedHeight(_line_height * 2 + 12)
        # The global stylesheet adds 22px right padding for filter funnels in the main table.
        # This dialog's table has no funnels, so reset to symmetric padding so text is not
        # pushed off-center or clipped against the left edge.
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
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        # `stretchLastSection` defaults to True and force-fits the last section into the leftover
        # width, overriding whatever the other sections were given.
        header.setStretchLastSection(False)
        header.setSectionResizeMode(
            self.COL_QUEUE, QHeaderView.ResizeMode.Stretch
        )
        for column in range(self.COL_DOWNLOADS, len(self.HEADERS)):
            # `Interactive`, **not** `ResizeToContents`. Qt's own size hint for a header cell is
            # style-dependent and can come up under the text - it measured "DOWNLOADS" a character
            # narrow and "Max at once" two, which is what trimmed the headers twice. Widening the
            # window cannot fix that, because the window's width is derived from these very
            # numbers, so the widths are measured from the live font and set explicitly in
            # `_fit_width_to_headers`.
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
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
        self._color_btn = QPushButton("Color…")
        self._delete_btn = QPushButton("Delete…")
        self._up_btn.clicked.connect(lambda: self._move_selected(-1))
        self._down_btn.clicked.connect(lambda: self._move_selected(+1))
        self._add_btn.clicked.connect(self._on_add)
        self._rename_btn.clicked.connect(self._on_rename)
        self._color_btn.clicked.connect(self._on_change_color)
        self._delete_btn.clicked.connect(self._on_delete)
        for btn in (self._up_btn, self._down_btn, self._add_btn, self._rename_btn, self._color_btn, self._delete_btn):
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
        self._fit_width_to_headers()

    #: Smallest useful queue-name column. The point of the stretch column is to be the one that
    #: gives up or gains width, so it needs a floor below which the name stops being readable.
    MIN_NAME_COLUMN_WIDTH = 170
    #: Never demand more than this, however wide the headers measure. On a small screen a window
    #: wider than the desktop is worse than an elided header, and the name column scrolls.
    MAX_DIALOG_WIDTH = 1400
    #: Per-column slack for the cell frame and a spin box's arrows, on top of the text.
    COLUMN_PADDING = 32

    #: Floor widths per column so headers and editor controls (spinboxes) have comfortable breathing room.
    MIN_COLUMN_WIDTHS = {
        COL_DOWNLOADS: 110,
        COL_MAX_CONCURRENT: 120,
        COL_DOWNLOAD_LIMIT: 160,
        COL_UPLOAD_LIMIT: 160,
    }

    def _header_column_widths(self):
        """Width each header needs, measured from this widget's own font.

        The widest *line*, because the headers are two lines deep and a header drawn into a
        section narrower than its longest line is elided at the edge - which is exactly how
        "DOWNLOADS" became "OWNLOADS".
        """
        header = self._table.horizontalHeader()
        font = QFont(header.font())
        if font.pixelSize() > 0:
            font.setPixelSize(max(font.pixelSize(), 12))
        else:
            font.setPointSize(max(font.pointSize(), 10))
        font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(font)

        style = self._table.style()
        # Qt paints a header inside `PM_HeaderMargin` of each edge, and a spin box's step buttons
        # need room besides, so the text width alone is not the section width.
        padding = 2 * style.pixelMetric(QStyle.PixelMetric.PM_HeaderMargin) + self.COLUMN_PADDING

        widths = []
        for column, text in enumerate(self.HEADERS):
            # Measure both original and uppercase because the stylesheet applies text-transform: uppercase
            widest = max(
                max(metrics.horizontalAdvance(line), metrics.horizontalAdvance(line.upper()))
                for line in text.split("\n")
            )
            widths.append(max(widest + padding, self.MIN_COLUMN_WIDTHS.get(column, 0)))
        return widths

    def _fit_width_to_headers(self):
        """Size the columns and the window to what the headers actually need.

        Three things have to be measured, and getting only the first two is what trimmed the
        headers twice:

        1. **The text**, per line - header width follows the user's font, its size and the display's
           DPI, so a hard-coded width cannot be right on more than one machine.
        2. **Qt's own header margin**, which is style-dependent and not included in (1).
        3. **The chrome around the table** - the layout margins, its frame, a scrollbar. The
           table does not get the whole window, and Qt resolves a shortfall by shrinking sections
           to fit the *viewport*, which is what clipped the first and last letter of every header.

        The column widths are therefore set here rather than left to `ResizeToContents`, whose hint
        was the thing under-measuring in the first place.
        """
        widths = self._header_column_widths()
        for column, width in enumerate(widths):
            if column != self.COL_QUEUE:
                self._table.setColumnWidth(column, width)

        margins = self.layout().contentsMargins()
        style = self._table.style()
        chrome = (
            margins.left()
            + margins.right()
            + 2 * self._table.frameWidth()
            + style.pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        )
        needed = sum(widths) + self.MIN_NAME_COLUMN_WIDTH + chrome

        if needed > self.MAX_DIALOG_WIDTH:
            # The clamp is about not demanding a window wider than a small desktop. But the name
            # column is `Stretch`, so it is the one that would be squeezed below its own header -
            # and the answer is not to elide it either. It is pinned to its measured width here and
            # the table scrolls, because a scrollbar is a smaller loss than a trimmed header.
            self._table.horizontalHeader().setSectionResizeMode(
                self.COL_QUEUE, QHeaderView.ResizeMode.Interactive
            )
            self._table.setColumnWidth(self.COL_QUEUE, widths[self.COL_QUEUE])
        else:
            self._table.horizontalHeader().setSectionResizeMode(
                self.COL_QUEUE, QHeaderView.ResizeMode.Stretch
            )

        self.setMinimumWidth(min(needed, self.MAX_DIALOG_WIDTH))
        target = min(max(needed, self.width()), self.MAX_DIALOG_WIDTH)
        if target != self.width():
            self.resize(target, self.height())

    def _refresh_note(self):
        """Explain how a queue limit interacts with the global limit, with the live values."""
        from my_idm.download_model import _format_speed

        general = self._manager.general_config
        net = self._manager.network_config
        global_max = general.effective_max_concurrent
        global_dl = net.download_limit or 0
        global_ul = net.upload_limit or 0
        dl_text = f"{_format_speed(global_dl)} (unlimited)" if global_dl > 0 else "unlimited"
        ul_text = f"{_format_speed(global_ul)} (unlimited)" if global_ul > 0 else "unlimited"
        self._note.setText(
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
                    "claims it. Its limits and colour are editable like any other."
                )
            self._table.setItem(row, self.COL_QUEUE, name_item)
            self._table.setCellWidget(row, self.COL_QUEUE, self._name_cell(queue))
            self._table.setItem(
                row, self.COL_DOWNLOADS, QTableWidgetItem(str(counts.get(queue.id, 0)))
            )
            # Each editor lives in the column it edits. There is no separate edit column: a blank
            # one with a control floating in it read as two unrelated things, and it made the
            # Default queue look un-editable because it was the only row without a control there.
            self._table.setCellWidget(row, self.COL_MAX_CONCURRENT, self._limit_editor(queue))
            self._table.setCellWidget(
                row, self.COL_DOWNLOAD_LIMIT, self._bandwidth_editor(queue, upload=False)
            )
            self._table.setCellWidget(
                row, self.COL_UPLOAD_LIMIT, self._bandwidth_editor(queue, upload=True)
            )
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

    def _bandwidth_editor(self, queue, upload: bool):
        """KB/s spin box for one direction of a queue's bandwidth ceiling.

        KB/s rather than bytes/s because that is the unit the global limit is set in everywhere
        else in this application, and a queue limit typed in a different unit from the global one
        it is compared against is a limit nobody can reason about.
        """
        stored = queue.upload_limit if upload else queue.download_limit
        spin = QSpinBox(self._table)
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
            lambda value, qid=queue.id, up=upload: self._on_bandwidth_changed(
                qid, up, value * 1024
            )
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

    def _on_change_color(self):
        queue_id = self._selected_queue_id()
        if queue_id:
            self._on_pick_color(queue_id)

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
        self._color_btn.setEnabled(bool(queue))
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

    def _on_bandwidth_changed(self, queue_id: str, upload: bool, value: int):
        """Persist one direction of a queue's ceiling, leaving the other alone.

        The caller passes only the direction it edited, so a spin box that emits during
        `_reload` cannot clear the opposite one - and, more importantly, so raising the download
        limit does not silently reset the upload limit to whatever it happened to be.
        """
        if self._loading:
            return
        queue = self._manager.get_queue(queue_id)
        if not queue:
            return
        download = value if not upload else queue.download_limit
        upload_limit = value if upload else queue.upload_limit
        self._manager.set_queue_limits(queue_id, download, upload_limit)
        self._refresh_note()

    def _move_selected(self, delta: int):
        queue_id = self._selected_queue_id()
        if not queue_id:
            return
        self._manager.move_queue_in_list(queue_id, delta)
        self._reload()
        self._select_queue_id(queue_id)

    def _on_add(self):
        dlg = AddQueueDialog(self, manager=self._manager)
        if not dlg.exec():
            return
        created, message = self._manager.create_queue(
            dlg.name.strip(), dlg.max_concurrent, dlg.color,
            dlg.download_limit_kb * 1024, dlg.upload_limit_kb * 1024,
        )
        self._result_message = message
        if created:
            self._reload()
            new_q = next((q for q in self._manager.get_queues() if q.name.lower() == dlg.name.strip().lower()), None)
            if new_q:
                self._select_queue_id(new_q.id)

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


class AddQueueDialog(QDialog):
    """Dialog to create a new named queue with custom name, concurrency limit, and color."""

    def __init__(
        self,
        parent=None,
        manager=None,
        initial_name: str = "",
        initial_color: str = "",
    ):
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle("Add New Queue")
        self.setMinimumWidth(380)
        self.setModal(True)

        from my_idm.resources import get_app_icon

        self.setWindowIcon(get_app_icon())

        # Pick default color from palette or manager/db if not provided
        if not initial_color and self._manager and hasattr(self._manager, "_db"):
            initial_color = self._manager._db._next_queue_color()
        if not initial_color:
            initial_color = DEFAULT_QUEUE_COLOR
        self._color = normalize_queue_color(initial_color) or DEFAULT_QUEUE_COLOR

        self._setup_ui(initial_name)

    def _setup_ui(self, initial_name: str):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        form_layout = QVBoxLayout()
        form_layout.setSpacing(10)

        # Queue Name
        name_label = QLabel("Queue Name:")
        name_label.setStyleSheet("font-weight: bold;")
        self._name_edit = QLineEdit(initial_name)
        self._name_edit.setPlaceholderText("e.g. Work, Torrents, Archive…")
        self._name_edit.textChanged.connect(self._update_preview)
        form_layout.addWidget(name_label)
        form_layout.addWidget(self._name_edit)

        # Concurrency Limit
        limit_label = QLabel("Max Concurrent Downloads (0 = Follow global limit):")
        limit_label.setStyleSheet("font-weight: bold;")
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(0, 99)
        self._limit_spin.setValue(3)
        self._limit_spin.setToolTip("0 = follow global limit (no cap of its own)")
        form_layout.addWidget(limit_label)
        form_layout.addWidget(self._limit_spin)

        # Bandwidth Limits
        dl_label = QLabel("Download Limit (KB/s, 0 = Follow global limit):")
        dl_label.setStyleSheet("font-weight: bold;")
        self._dl_spin = QSpinBox()
        self._dl_spin.setRange(0, 10_000_000)
        self._dl_spin.setSingleStep(64)
        self._dl_spin.setSuffix(" KB/s")
        self._dl_spin.setToolTip(
            "Ceiling on this queue's total download rate.\n"
            "0 = follow the global limit (no cap of its own).\n"
            "Where both are set, the tighter one wins."
        )
        form_layout.addWidget(dl_label)
        form_layout.addWidget(self._dl_spin)

        ul_label = QLabel("Upload Limit (KB/s, 0 = Follow global limit):")
        ul_label.setStyleSheet("font-weight: bold;")
        self._ul_spin = QSpinBox()
        self._ul_spin.setRange(0, 10_000_000)
        self._ul_spin.setSingleStep(64)
        self._ul_spin.setSuffix(" KB/s")
        self._ul_spin.setToolTip(
            "Ceiling on this queue's total upload (seeding) rate.\n"
            "0 = follow the global limit (no cap of its own).\n"
            "Where both are set, the tighter one wins."
        )
        form_layout.addWidget(ul_label)
        form_layout.addWidget(self._ul_spin)

        # Color Picker Section
        color_label = QLabel("Queue Color:")
        color_label.setStyleSheet("font-weight: bold;")
        form_layout.addWidget(color_label)

        color_row = QHBoxLayout()
        color_row.setSpacing(8)

        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(30, 30)
        self._color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color_btn.setToolTip("Click to choose a custom color")
        self._color_btn.clicked.connect(self._on_pick_custom_color)
        color_row.addWidget(self._color_btn)

        # Palette quick-picker buttons
        from my_idm.database import QUEUE_COLOR_PALETTE

        for pal_col in QUEUE_COLOR_PALETTE:
            pal_btn = QPushButton()
            pal_btn.setFixedSize(22, 22)
            pal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            pal_btn.setStyleSheet(
                f"QPushButton {{ background-color: {pal_col}; border: 1px solid #555; border-radius: 4px; }}"
                f"QPushButton:hover {{ border: 2px solid #fff; }}"
            )
            pal_btn.setToolTip(f"Select color {pal_col}")
            pal_btn.clicked.connect(
                lambda _checked=False, c=pal_col: self._set_color(c)
            )
            color_row.addWidget(pal_btn)

        color_row.addStretch()
        form_layout.addLayout(color_row)

        layout.addLayout(form_layout)

        # Dialog buttons
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._update_preview()

    def _set_color(self, color: str):
        self._color = normalize_queue_color(color) or DEFAULT_QUEUE_COLOR
        self._update_preview()

    def _on_pick_custom_color(self):
        from PySide6.QtWidgets import QColorDialog

        current = QColor(self._color)
        chosen = QColorDialog.getColor(current, self, "Select Queue Color")
        if chosen.isValid():
            self._set_color(chosen.name())

    def _update_preview(self):
        char = self.name.strip()[:1].upper()
        qc = QColor(self._color)
        luminance = (
            0.299 * qc.red() + 0.587 * qc.green() + 0.114 * qc.blue()
        ) / 255.0
        text_color = "#000000" if luminance > 0.65 else "#ffffff"
        self._color_btn.setText(char)
        self._color_btn.setStyleSheet(
            f"QPushButton {{ background-color: {self._color}; color: {text_color}; "
            f"font-weight: bold; font-size: 13px; border: 1px solid #555; border-radius: 4px; }}"
            f"QPushButton:hover {{ border: 2px solid #fff; }}"
        )

    def _on_accept(self):
        if not self.name.strip():
            QMessageBox.warning(self, "Invalid Name", "Queue name cannot be empty.")
            self._name_edit.setFocus()
            return
        self.accept()

    @property
    def name(self) -> str:
        return self._name_edit.text()

    @property
    def max_concurrent(self) -> int:
        return self._limit_spin.value()

    @property
    def download_limit_kb(self) -> int:
        return self._dl_spin.value()

    @property
    def upload_limit_kb(self) -> int:
        return self._ul_spin.value()

    @property
    def color(self) -> str:
        return self._color

