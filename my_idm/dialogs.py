"""Dialog windows for My-IDM."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from my_idm.config import GeneralConfig, DEFAULT_DOWNLOADS_DIR, TorConfig

DEFAULT_SAVE_PATH = DEFAULT_DOWNLOADS_DIR


class AddDownloadDialog(QDialog):
    """Dialog to add a new download (URL, magnet link, or .torrent file)."""

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
        url_group = QGroupBox("URL / Magnet Link")
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

        layout.addWidget(url_group)

        # Save location
        save_group = QGroupBox("Save Location")
        save_layout = QVBoxLayout(save_group)
        save_layout.setSpacing(6)

        path_row = QHBoxLayout()
        self._save_edit = QLineEdit(self._save_path)
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
            self, "Select Save Directory", self._save_edit.text()
        )
        if path:
            self._save_edit.setText(path)

    @staticmethod
    def _is_valid_download_url(text: str) -> bool:
        if not text or len(text) > 4096:
            return False
        trimmed = text.strip()
        lower = trimmed.lower()
        if lower.startswith(("http://", "https://", "ftp://", "magnet:?")):
            return True
        if lower.endswith(".torrent") and (os.path.isfile(trimmed) or lower.startswith("file://")):
            return True
        return False

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

    def _accept(self):
        raw_text = self._url_edit.toPlainText().strip()
        self._urls = [l.strip() for l in raw_text.splitlines() if l.strip()]
        self._url = self._urls[0] if self._urls else ""
        self._save_path = self._save_edit.text().strip()
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

    def __init__(self, current_path: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Move Download")
        self.setMinimumWidth(450)
        self.setModal(True)

        self._new_path = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        layout.addWidget(QLabel("Select new save directory:"))

        path_layout = QHBoxLayout()
        self._path_edit = QLineEdit(current_path)
        path_layout.addWidget(self._path_edit)

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
            self, "Select Directory", self._path_edit.text()
        )
        if path:
            self._path_edit.setText(path)

    def _accept(self):
        self._new_path = self._path_edit.text().strip()
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

        self._delete_files = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        items = "this download" if count == 1 else f"these {count} downloads"
        layout.addWidget(QLabel(
            f"Are you sure you want to remove {items}?"
        ))

        self._files_cb = QCheckBox("Also delete downloaded files from disk (move to Trash)")
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
