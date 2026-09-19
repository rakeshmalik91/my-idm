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
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

DEFAULT_SAVE_PATH = str(Path.home() / "Downloads")


class AddDownloadDialog(QDialog):
    """Dialog to add a new download (URL, magnet link, or .torrent file)."""

    def __init__(self, parent=None, initial_url: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Add Download")
        self.setMinimumWidth(550)
        self.setModal(True)

        self._url = ""
        self._save_path = DEFAULT_SAVE_PATH
        self._num_segments = 8

        self._setup_ui()
        self._prefill_url(initial_url)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # URL / Magnet input
        url_group = QGroupBox("URL / Magnet Link")
        url_layout = QVBoxLayout(url_group)

        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText(
            "Paste URL, magnet link, or browse for .torrent file..."
        )
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
        save_layout = QHBoxLayout(save_group)

        self._save_edit = QLineEdit(self._save_path)
        save_layout.addWidget(self._save_edit)

        save_browse_btn = QPushButton("Browse …")
        save_browse_btn.clicked.connect(self._browse_save_path)
        save_layout.addWidget(save_browse_btn)

        layout.addWidget(save_group)

        # Options
        options_group = QGroupBox("Options")
        options_layout = QHBoxLayout(options_group)

        options_layout.addWidget(QLabel("Segments:"))
        self._seg_spin = QSpinBox()
        self._seg_spin.setRange(1, 32)
        self._seg_spin.setValue(8)
        self._seg_spin.setToolTip(
            "Number of parallel connections for HTTP downloads"
        )
        options_layout.addWidget(self._seg_spin)
        options_layout.addStretch()

        layout.addWidget(options_group)

        # Buttons
        btn_layout = QHBoxLayout()
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

    def _browse_torrent(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Torrent File", "",
            "Torrent Files (*.torrent);;All Files (*)",
        )
        if path:
            self._url_edit.setText(path)

    def _browse_save_path(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Save Directory", self._save_edit.text()
        )
        if path:
            self._save_edit.setText(path)

    @staticmethod
    def _is_valid_download_url(text: str) -> bool:
        if not text or len(text) > 4096 or "\n" in text or "\r" in text:
            return False
        lower = text.lower()
        if lower.startswith(("http://", "https://", "ftp://", "magnet:?")):
            return True
        if lower.endswith(".torrent") and (os.path.isfile(text) or lower.startswith("file://")):
            return True
        return False

    def _prefill_url(self, initial_url: str = ""):
        candidate = initial_url.strip() if initial_url else ""
        if not candidate:
            clipboard = QGuiApplication.clipboard()
            if clipboard:
                text = (clipboard.text() or "").strip()
                if self._is_valid_download_url(text):
                    candidate = text

        if candidate:
            self._url_edit.setText(candidate)
            self._url_edit.selectAll()

    def _accept(self):
        self._url = self._url_edit.text().strip()
        self._save_path = self._save_edit.text().strip()
        self._num_segments = self._seg_spin.value()
        if self._url:
            self.accept()

    @property
    def url(self) -> str:
        return self._url

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

        self._files_cb = QCheckBox("Also delete downloaded files from disk")
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
