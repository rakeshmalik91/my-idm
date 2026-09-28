"""YouTube download dialog: URL analysis, format selection, and dispatch.

Metadata extraction runs on a worker thread so the UI never blocks, and the
actual download is handed to :class:`~my_idm.manager.DownloadManager`, which
decides between Mode A (HTTPEngine) and Mode B (yt-dlp + ffmpeg merge).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (
    QObject,
    QTimer,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from my_idm.config import (
    ExternalToolsConfig,
    MAX_YTDLP_PLAYLIST_LIMIT,
    clamp_ytdlp_playlist_limit,
)
from my_idm import youtube_tool as ytt
from my_idm.utils import normalize_path

log = logging.getLogger(__name__)

# label -> (yt-dlp selector, max height or None for audio-only)
QUALITY_PRESETS: list[tuple[str, str, Optional[int]]] = [
    ("Best available (recommended)", "bestvideo*+bestaudio/best", None),
    ("Best 1080p", "bestvideo[height<=1080]+bestaudio/best", 1080),
    ("Best 720p", "bestvideo[height<=720]+bestaudio/best", 720),
    ("Best 480p", "bestvideo[height<=480]+bestaudio/best", 480),
    ("Best 360p", "bestvideo[height<=360]+bestaudio/best", 360),
    ("Audio only (M4A)", "bestaudio[ext=m4a]/bestaudio", 0),
    ("Audio only (Opus)", "bestaudio[ext=webm]/bestaudio", 0),
]

COL_MODE, COL_RES, COL_FPS, COL_VCODEC, COL_ACODEC, COL_SIZE, COL_EXT = range(7)
COLUMN_HEADERS = ["Mode", "Resolution", "FPS", "Video", "Audio", "Size", "Ext"]


class _SignalBridge(QObject):
    """Marshals worker results back onto the GUI thread.

    The bridge lives in the thread that created the dialog, so emitting from a
    plain Python thread produces a queued connection automatically.
    """

    finished = Signal(object)
    failed = Signal(str, str)


# Daemon threads are tracked so their objects cannot be collected mid-flight.
_LIVE_THREADS: set = set()


class _ExtractWorker:
    """Runs yt-dlp metadata extraction on a daemon thread.

    A plain thread is used rather than ``QThread`` on purpose: a ``QThread`` whose
    C++ object is destroyed while ``run()`` is still executing aborts the process,
    which is exactly what happened when the dialog was closed mid-analysis.
    Daemon threads carry no such ownership requirement.
    """

    def __init__(
        self,
        url: str,
        config: ExternalToolsConfig,
        playlist: bool,
        cancel_event=None,
        limit: Optional[int] = None,
    ):
        self._url = url
        self._config = config
        self._playlist = playlist
        self._limit = limit
        self._cancel_event = cancel_event
        self.bridge = _SignalBridge()
        self._thread: Optional[threading.Thread] = None

    def _cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="yt-extract", daemon=True)
        _LIVE_THREADS.add(self._thread)
        self._thread.start()

    def _finish(self) -> None:
        if self._thread is not None:
            _LIVE_THREADS.discard(self._thread)

    def _run(self) -> None:
        try:
            if self._playlist:
                result = ytt.extract_playlist(
                    self._url, self._config,
                    cancel_check=self._cancelled,
                    limit=self._limit,
                )
                videos = result.videos
                self.bridge.finished.emit(result)
            else:
                videos = [ytt.extract_metadata(self._url, self._config)]
                self.bridge.finished.emit(videos)
        except ytt.YouTubeToolError as exc:
            self.bridge.failed.emit(exc.kind, str(exc))
            return
        except Exception as exc:
            log.exception("Unexpected YouTube extraction failure")
            self.bridge.failed.emit("error", str(exc))
            return
        finally:
            self._finish()

        if self._cancelled():
            self.bridge.failed.emit("cancelled", "Analysis cancelled.")
            return

    def cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class YouTubeDialog(QDialog):
    """Analyse a YouTube URL and queue a download with a chosen quality."""

    def __init__(self, parent=None, manager=None, initial_url: str = ""):
        super().__init__(parent)
        self._manager = manager
        if self._manager is None and parent is not None:
            self._manager = getattr(parent, "_manager", None)

        self.setWindowTitle("Download YouTube Video")
        self.setMinimumSize(880, 820)
        self.setModal(True)

        from my_idm.resources import get_app_icon
        self.setWindowIcon(get_app_icon())

        self._config = (
            self._manager.external_tools_config
            if self._manager is not None and hasattr(self._manager, "external_tools_config")
            else ExternalToolsConfig.load()
        )

        self._videos: list[ytt.YouTubeMetadata] = []
        self._current: Optional[ytt.YouTubeMetadata] = None
        self._playlist_total = 0
        self._playlist_truncated = False
        self._worker: Optional[_ExtractWorker] = None
        self._cancel_event: Optional[threading.Event] = None
        self._net = QNetworkAccessManager(self)
        self._thumb_reply: Optional[QNetworkReply] = None
        self._format_ids: list[str] = []
        self._selected_format_id: str = ""
        self._batch_mode = False
        self._closing = False
        self._submitted = False

        self._build_ui()
        self._prefill(initial_url)

    # -- construction --------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(18, 18, 18, 18)

        if not self._config.ytdlp_enabled:
            warn = QLabel("⚠️ YouTube integration is disabled in Settings → External Tools → YouTube.")
            warn.setStyleSheet("color: #f59e0b; font-weight: bold;")
            layout.addWidget(warn)

        if not ytt.check_ytdlp_available(self._config):
            missing = QLabel(
                "⚠️ yt-dlp was not found.\n"
                f"{ytt.YTDLP_INSTALL_HINT}\n\n"
                "or set its path in Settings → External Tools → YouTube."
            )
            missing.setWordWrap(True)
            missing.setStyleSheet("color: #ef4444;")
            layout.addWidget(missing)

        layout.addWidget(self._build_url_group())
        layout.addWidget(self._build_info_group(), 2)
        layout.addWidget(self._build_format_group())
        layout.addWidget(self._build_options_group())
        layout.addLayout(self._build_buttons())

        self._set_analyzing(False)

    def _build_url_group(self) -> QWidget:
        group = QGroupBox("Video URL")
        inner = QHBoxLayout(group)

        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText(
            "https://www.youtube.com/watch?v=…  (also supports youtu.be, /shorts/, playlists)"
        )
        self._url_edit.setClearButtonEnabled(True)
        self._url_edit.returnPressed.connect(self._on_analyze)
        inner.addWidget(self._url_edit, 1)

        self._paste_btn = QPushButton("Paste")
        self._paste_btn.clicked.connect(self._on_paste)
        inner.addWidget(self._paste_btn)

        self._analyze_btn = QPushButton("Analyze ▶")
        self._analyze_btn.setObjectName("primaryButton")
        self._analyze_btn.clicked.connect(self._on_analyze)
        inner.addWidget(self._analyze_btn)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #8fa0b5;")
        inner.addWidget(self._status_lbl)
        return group

    def _build_info_group(self) -> QWidget:
        group = QGroupBox("Video")
        outer = QHBoxLayout(group)

        # -- Video details (thumbnail + metadata) -- hidden in playlist mode
        self._video_details = QWidget()
        details_layout = QHBoxLayout(self._video_details)
        details_layout.setContentsMargins(0, 0, 0, 0)

        self._thumb_label = QLabel()
        self._thumb_label.setFixedSize(200, 112)
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setStyleSheet(
            "background:#161b22; border:1px solid #30363d; border-radius:4px; color:#8b949e;"
        )
        self._thumb_label.setText("No preview")
        details_layout.addWidget(self._thumb_label)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._title_lbl = QLabel("—")
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._uploader_lbl = QLabel("—")
        self._duration_lbl = QLabel("—")
        self._date_lbl = QLabel("—")
        form.addRow("Title:", self._title_lbl)
        form.addRow("Uploader:", self._uploader_lbl)
        form.addRow("Duration:", self._duration_lbl)
        form.addRow("Uploaded:", self._date_lbl)
        details_layout.addLayout(form, 1)
        outer.addWidget(self._video_details, 1)

        # -- Playlist list (takes full width when visible) --
        self._playlist_list = QListWidget()
        self._playlist_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._playlist_list.currentRowChanged.connect(self._on_playlist_row_changed)
        self._playlist_list.itemChanged.connect(self._on_playlist_item_changed)
        # A batch can hold up to `ytdlp_playlist_limit` entries, so give the list
        # plenty of room. The minimum is deliberately modest: the widget sits in a
        # stretch-1 slot, so it absorbs the spare height on a tall dialog and
        # shrinks to this floor on a short one. A large hard minimum instead
        # overflows the slot and the list paints over the buttons below it.
        self._playlist_list.setMinimumHeight(170)
        self._playlist_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._playlist_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._playlist_list.setUniformItemSizes(True)
        self._playlist_list.setWordWrap(True)

        self._sel_all_btn = QPushButton("Select all")
        self._sel_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        self._sel_none_btn = QPushButton("Clear")
        self._sel_none_btn.clicked.connect(lambda: self._set_all_checked(False))

        self._playlist_btns = QWidget()
        # Fixed vertically so the row is never squeezed against the list, and
        # sized to the button so the surrounding layout cannot shrink it.
        self._playlist_btns.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        pbl = QHBoxLayout(self._playlist_btns)
        pbl.setContentsMargins(0, 0, 0, 0)
        pbl.addWidget(self._sel_all_btn)
        pbl.addWidget(self._sel_none_btn)
        pbl.addStretch()

        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(10)
        wl.addWidget(self._playlist_list, 1)
        wl.addWidget(self._playlist_btns, 0)
        self._playlist_wrap = wrapper
        self._playlist_wrap.setVisible(False)
        self._playlist_btns.setVisible(False)
        outer.addWidget(self._playlist_wrap, 1)
        return group

    def _build_format_group(self) -> QWidget:
        group = QGroupBox("Quality")
        inner = QVBoxLayout(group)

        preset_row = QHBoxLayout()
        self._preset_lbl = QLabel("Preset:")
        preset_row.addWidget(self._preset_lbl)
        self._preset_combo = QComboBox()
        for label, _selector, _h in QUALITY_PRESETS:
            self._preset_combo.addItem(label)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        preset_row.addWidget(self._preset_combo, 1)

        self._ffmpeg_lbl = QLabel("")
        preset_row.addWidget(self._ffmpeg_lbl)
        inner.addLayout(preset_row)

        # Shown in playlist/batch mode: the quality applies to every selected
        # video, so it is stated explicitly instead of implying a per-video list.
        self._batch_quality_lbl = QLabel("")
        self._batch_quality_lbl.setWordWrap(True)
        self._batch_quality_lbl.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        self._batch_quality_lbl.setVisible(False)
        inner.addWidget(self._batch_quality_lbl)

        self._table = QTableWidget(0, len(COLUMN_HEADERS))
        self._table.setHorizontalHeaderLabels(COLUMN_HEADERS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setMinimumHeight(160)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        header = self._table.horizontalHeader()
        for col in (COL_RES, COL_FPS, COL_SIZE, COL_EXT):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_VCODEC, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_ACODEC, QHeaderView.ResizeMode.Stretch)
        inner.addWidget(self._table)

        self._mode_lbl = QLabel("")
        self._mode_lbl.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        inner.addWidget(self._mode_lbl)
        return group

    def _build_options_group(self) -> QWidget:
        group = QGroupBox("Options")
        inner = QVBoxLayout(group)

        self._embed_thumb_cb = QCheckBox("Embed thumbnail in the downloaded file")
        self._embed_thumb_cb.setChecked(self._config.ytdlp_embed_thumbnail)
        self._embed_thumb_cb.setToolTip(
            "Requires ffmpeg. Only used by yt-dlp native downloads."
        )
        inner.addWidget(self._embed_thumb_cb)

        subs_row = QHBoxLayout()
        self._embed_subs_cb = QCheckBox("Download and embed subtitles")
        self._embed_subs_cb.setChecked(self._config.ytdlp_embed_subtitles)
        subs_row.addWidget(self._embed_subs_cb)

        self._subs_langs_edit = QLineEdit(self._config.ytdlp_subtitle_langs)
        self._subs_langs_edit.setPlaceholderText("en, ja")
        self._subs_langs_edit.setEnabled(self._config.ytdlp_embed_subtitles)
        self._subs_langs_edit.setMaximumWidth(180)
        subs_row.addWidget(QLabel("Languages:"))
        subs_row.addWidget(self._subs_langs_edit)
        subs_row.addStretch()
        self._embed_subs_cb.toggled.connect(self._subs_langs_edit.setEnabled)
        inner.addLayout(subs_row)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Save to:"))
        default_path = (
            self._config.ytdlp_last_save_path
            or self._manager.general_config.get_effective_save_path()
            if self._manager is not None and hasattr(self._manager, "general_config")
            else self._config.ytdlp_last_save_path
        )
        self._save_edit = QComboBox()
        self._save_edit.setEditable(True)
        self._save_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._save_edit.setEditText(default_path or str(Path.home() / "Downloads"))
        self._save_edit.setMinimumWidth(360)
        path_row.addWidget(self._save_edit, 1)

        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse_save_path)
        path_row.addWidget(browse)
        inner.addLayout(path_row)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        inner.addWidget(self._progress)
        return group

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._ffmpeg_hint = QLabel("")
        self._ffmpeg_hint.setStyleSheet("color: #8fa0b5; font-size: 11px;")
        row.addWidget(self._ffmpeg_hint, 1)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        self._download_btn = QPushButton("⬇ Download")
        self._download_btn.setObjectName("primaryButton")
        self._download_btn.setDefault(True)
        self._download_btn.setEnabled(False)
        self._download_btn.clicked.connect(self._on_download)
        row.addWidget(self._download_btn)
        return row

    def _prefill(self, initial_url: str = ""):
        if initial_url:
            self._url_edit.setText(initial_url.strip())
        else:
            clipboard = self._clipboard_text()
            if clipboard and ytt.detect_youtube_url(clipboard):
                self._url_edit.setText(clipboard.strip())
        if self._url_edit.text().strip():
            QTimer.singleShot(0, self._on_analyze)

    @staticmethod
    def _clipboard_text() -> str:
        from PySide6.QtGui import QGuiApplication
        clipboard = QGuiApplication.clipboard()
        return (clipboard.text() or "") if clipboard else ""

    # -- extraction ----------------------------------------------------------

    def _on_paste(self):
        text = self._clipboard_text()
        if text:
            self._url_edit.setText(text.strip())
            self._on_analyze()

    def _on_analyze(self):
        url = ytt.detect_youtube_url(self._url_edit.text() or "")
        if not url:
            url = (self._url_edit.text() or "").strip()
            if not url:
                self._status_lbl.setText("Enter a YouTube URL first.")
                return
            self._status_lbl.setText("Not a recognised YouTube URL — trying anyway…")

        self._url_edit.setText(url)
        self._discard_worker()

        cfg = self._config
        playlist = ytt.is_playlist_url(url)
        self._set_analyzing(True, "Analyzing…" if not playlist else "Reading playlist…")

        cancel_event = threading.Event()
        self._cancel_event = cancel_event

        self._worker = _ExtractWorker(url, cfg, playlist, cancel_event, limit=self._playlist_limit())
        self._worker.bridge.finished.connect(self._on_extract_finished)
        self._worker.bridge.failed.connect(self._on_extract_failed)
        self._worker.start()

    def _playlist_limit(self) -> int:
        """Playlist entry cap — always load all available entries."""
        return MAX_YTDLP_PLAYLIST_LIMIT

    def _set_analyzing(self, active: bool, message: str = ""):
        self._analyze_btn.setEnabled(not active)
        self._analyze_btn.setText("Analyzing…" if active else "Analyze ▶")
        self._status_lbl.setText(message)
        if not active:
            self._status_lbl.setText("")

    def _detach_worker(self) -> None:
        """Cancel and forget the analysis worker.

        The worker is a daemon thread, so abandoning it while it is mid-request
        is safe: nothing is destroyed underneath it and the process cannot abort.
        Its bridge signals are disconnected so a late result cannot touch a
        dialog that is already gone.
        """
        worker = self._worker
        self._worker = None
        if worker is None:
            return

        worker.cancel()
        for signal, slot in (
            (worker.bridge.finished, self._on_extract_finished),
            (worker.bridge.failed, self._on_extract_failed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _discard_worker(self):
        self._detach_worker()
        self._cancel_event = None

    @Slot(object)
    def _on_extract_finished(self, payload):
        if self._closing:
            return
        self._set_analyzing(False)
        self._discard_worker()

        if isinstance(payload, ytt.PlaylistResult):
            self._videos = list(payload.videos)
            self._playlist_total = payload.total
            self._playlist_truncated = payload.truncated
        else:
            self._videos = list(payload or [])
            self._playlist_total = len(self._videos)
            self._playlist_truncated = False

        if not self._videos:
            self._status_lbl.setText("No downloadable items found.")
            return

        self._current = self._videos[0]
        self._fill_info(self._current)
        self._populate_table(self._current)
        self._download_btn.setEnabled(True)

        if len(self._videos) > 1:
            self._show_playlist_list()
        else:
            self._batch_mode = False
            self._video_details.setVisible(True)
            self._playlist_list.setVisible(False)
            self._playlist_wrap.setVisible(False)
            self._playlist_list.clear()

    @Slot(str, str)
    def _on_extract_failed(self, kind: str, message: str):
        if self._closing:
            return
        self._discard_worker()
        if kind == "cancelled":
            self._set_analyzing(False)
            return

        self._set_analyzing(False)
        self._download_btn.setEnabled(False)
        self._table.setRowCount(0)

        titles = {
            "not_installed": "yt-dlp Not Installed",
            "auth": "Authentication Required",
            "age_restricted": "Age-Restricted Video",
            "geo_restricted": "Geo-Restricted Video",
            "unsupported": "Unsupported Site",
            "rate_limited": "Rate Limited",
        }
        QMessageBox.critical(
            self,
            titles.get(kind, "YouTube Analysis Failed"),
            message or "Could not analyse this URL.",
        )

    def _fill_info(self, md: ytt.YouTubeMetadata):
        self._title_lbl.setText(md.title or "—")
        self._uploader_lbl.setText(md.uploader or "—")
        self._duration_lbl.setText(md.duration_label or "—")
        self._date_lbl.setText(md.upload_date_label or "—")
        self._load_thumbnail(md.thumbnail)

    def _show_playlist_list(self):
        """Populate the batch checkbox list.

        In playlist mode the video-detail panel (thumbnail / title / uploader)
        is hidden so the checkbox list can use the full width of the group box.
        The Select all / Clear buttons are only meaningful with more than one
        entry, so they stay hidden for a single-item list.
        """
        self._playlist_list.clear()
        for md in self._videos:
            label = f"{md.title}"
            if md.duration_label:
                label += f"  ({md.duration_label})"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, md)
            self._playlist_list.addItem(item)

        count = self._playlist_list.count()
        self._batch_mode = count > 1
        show = count > 1

        # Hide video details and give the list full width in playlist mode
        self._video_details.setVisible(not show)
        self._playlist_list.setVisible(show)
        self._playlist_wrap.setVisible(show)
        self._playlist_btns.setVisible(show)
        self._set_batch_quality_ui()

        if count == 0:
            self._status_lbl.setText("No videos found in this playlist.")
            return

        if self._playlist_truncated:
            self._status_lbl.setText(
                f"Showing {count} of {self._playlist_total} videos."
            )
        elif count > 1:
            self._status_lbl.setText(f"{count} videos in this playlist.")

    def _set_batch_quality_ui(self):
        """Show one global quality control for a batch, and the per-video table otherwise.

        Only the first video of a playlist is fully resolved (resolving each entry
        would cost an extra request per video), so a per-format table would
        misrepresent the rest. The preset applies to every selected video, and the
        table is hidden so the UI does not imply per-video formats exist.
        """
        batch = self._batch_mode
        self._table.setVisible(not batch)
        self._batch_quality_lbl.setVisible(batch)
        self._preset_lbl.setText("Quality (all selected videos):" if batch else "Preset:")
        if not batch:
            return

        count = self._playlist_list.count()
        label, selector, _height = QUALITY_PRESETS[max(0, self._preset_combo.currentIndex())]
        checked = self._checked_playlist_count()
        self._batch_quality_lbl.setText(
            f"“{label}” applies to all {count} listed video{'s' if count != 1 else ''} "
            f"({checked} selected). Each downloads separately.  Format: {selector}"
        )
        self._mode_lbl.setText("")

    def _set_all_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self._playlist_list.count()):
            self._playlist_list.item(row).setCheckState(state)
        if self._batch_mode:
            self._set_batch_quality_ui()

    def _selected_playlist_items(self) -> list[ytt.YouTubeMetadata]:
        out = []
        for row in range(self._playlist_list.count()):
            item = self._playlist_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                md = item.data(Qt.ItemDataRole.UserRole)
                if md is not None:
                    out.append(md)
        return out

    def _checked_playlist_count(self) -> int:
        """Number of ticked playlist entries (pure read, safe to call from the UI updater)."""
        return sum(
            1
            for row in range(self._playlist_list.count())
            if self._playlist_list.item(row).checkState() == Qt.CheckState.Checked
        )

    def _on_playlist_item_changed(self, _item):
        """Keep the "N selected" count live when a checkbox is toggled."""
        if self._batch_mode:
            self._set_batch_quality_ui()

    def _on_playlist_row_changed(self, row: int):
        """Update the info panel when a different playlist entry is clicked.

        The per-format table is intentionally left alone in batch mode: only the
        first video is fully resolved (resolving each one would cost an extra
        request per video), so repopulating from an unresolved entry would blank
        the table and disable the download button. The quality preset applies to
        every selected video instead.
        """
        if row < 0:
            return
        item = self._playlist_list.item(row)
        if item is None:
            return
        md = item.data(Qt.ItemDataRole.UserRole)
        if md is not None:
            self._current = md
            self._fill_info(md)
            if not self._batch_mode:
                self._populate_table(md)


    # -- thumbnail -----------------------------------------------------------

    def _load_thumbnail(self, url: str):
        if self._thumb_reply is not None:
            try:
                self._thumb_reply.abort()
            except RuntimeError:
                pass
            self._thumb_reply = None
        self._thumb_label.setText("Loading…")
        if not url:
            self._thumb_label.setText("No preview")
            return
        request = QNetworkRequest(QUrl(url))
        reply = self._net.get(request)
        self._thumb_reply = reply
        reply.finished.connect(lambda: self._on_thumb_ready(reply))

    def _on_thumb_ready(self, reply: QNetworkReply):
        if self._thumb_reply is reply:
            self._thumb_reply = None
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._thumb_label.setText("No preview")
            reply.deleteLater()
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(reply.readAll()):
            self._thumb_label.setText("No preview")
        else:
            self._thumb_label.setPixmap(
                pixmap.scaled(
                    self._thumb_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        reply.deleteLater()

    # -- format table --------------------------------------------------------

    def _populate_table(self, md: ytt.YouTubeMetadata):
        formats = sorted(
            md.formats,
            key=lambda f: (
                0 if f.is_video_only else 1 if f.is_audio_only else 2,
                -(f.fps or 0),
                -(f.filesize or 0),
            ),
        )
        self._table.setRowCount(len(formats))
        self._format_ids = [f.format_id for f in formats]
        for row, fmt in enumerate(formats):
            if fmt.is_video_only:
                mode = "B (merge)"
            elif fmt.is_muxed:
                mode = "A (fast)"
            elif fmt.is_audio_only:
                mode = "A (audio)"
            else:
                mode = "B"
            cells = [
                mode,
                fmt.display_resolution,
                fmt.display_fps,
                fmt.vcodec,
                fmt.acodec,
                fmt.quality_label,
                fmt.ext or "—",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col == COL_MODE:
                    item.setForeground(Qt.GlobalColor.green if mode.startswith("A") else Qt.GlobalColor.yellow)
                self._table.setItem(row, col, item)

        if not formats:
            self._format_ids = []
            self._selected_format_id = ""
            self._mode_lbl.setText("No formats available.")
            self._download_btn.setEnabled(False)
            return

        self._download_btn.setEnabled(True)

        has_ffmpeg = ytt.check_ffmpeg_available(self._config)
        self._ffmpeg_lbl.setText(
            "ffmpeg: ✓ found" if has_ffmpeg else "ffmpeg: ✗ not found — merging unavailable"
        )
        self._ffmpeg_hint.setText(
            "" if has_ffmpeg
            else "Merging high-quality video needs ffmpeg. Set its path in Settings → External Tools → YouTube."
        )
        self._on_preset_changed(self._preset_combo.currentIndex())

    def _current_preset(self) -> tuple[str, Optional[int]]:
        index = max(0, self._preset_combo.currentIndex())
        return QUALITY_PRESETS[index][1], QUALITY_PRESETS[index][2]

    def _on_preset_changed(self, _index: int = 0):
        if self._batch_mode:
            self._set_batch_quality_ui()
            return
        if self._table.rowCount() == 0:
            self._mode_lbl.setText("")
            return
        _selector, max_height = self._current_preset()
        row = self._pick_row_for_preset(max_height)
        if row >= 0:
            self._table.selectRow(row)

    def _pick_row_for_preset(self, max_height: Optional[int]) -> int:
        """Choose the table row best matching the active preset."""
        best_row = -1
        best_height = -1
        first_audio_row = -1

        for row in range(self._table.rowCount()):
            mode_item = self._table.item(row, COL_MODE)
            res_item = self._table.item(row, COL_RES)
            if not mode_item or not res_item:
                continue
            is_audio = mode_item.text().startswith("A (audio")
            if is_audio and first_audio_row < 0:
                first_audio_row = row
            res = res_item.text()

            if max_height == 0:
                if is_audio:
                    return row
                continue

            if is_audio or not res.endswith("p"):
                continue
            try:
                height = int(res[:-1])
            except ValueError:
                continue
            if max_height is not None and height > max_height:
                continue
            if height > best_height:
                best_height = height
                best_row = row

        if best_row >= 0:
            return best_row
        if first_audio_row >= 0:
            return first_audio_row
        return 0

    def _on_row_selected(self):
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return
        row = rows[0].row()
        mode_item = self._table.item(row, COL_MODE)
        res_item = self._table.item(row, COL_RES)
        if not mode_item or not res_item:
            return

        if self._current is None:
            return
        self._selected_format_id = self._format_id_at(row)

        mode = mode_item.text()
        if mode.startswith("A"):
            note = (
                f"Selected: {res_item.text()} — downloaded by My-IDM (segmented, resumable, "
                f"VPN/Tor aware). Format {self._selected_format_id}."
            )
        else:
            note = (
                f"Selected: {res_item.text()} — downloaded by yt-dlp with ffmpeg merging. "
                f"Format {self._selected_format_id}."
            )
        self._mode_lbl.setText(note)

    def _format_id_at(self, row: int) -> str:
        if self._current is None:
            return ""
        return self._format_ids[row] if 0 <= row < len(self._format_ids) else ""

    # -- submit --------------------------------------------------------------

    def _on_browse_save_path(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select Save Directory", self._save_edit.currentText()
        )
        if path:
            self._save_edit.setEditText(normalize_path(path))

    def _on_download(self):
        if not self._videos:
            return
        save_path = normalize_path(self._save_edit.currentText().strip())
        if not save_path:
            QMessageBox.warning(self, "Save Location Required", "Choose a folder to save into.")
            return

        if self._batch_mode and self._playlist_list.count() > 0:
            targets = self._selected_playlist_items()
            if not targets:
                QMessageBox.warning(self, "Nothing Selected", "Select at least one video to download.")
                return
        else:
            targets = [self._current] if self._current else []

        selector, max_height = self._current_preset()
        if not self._batch_mode and self._selected_format_id and max_height is not None and max_height > 0:
            row = self._table.currentRow()
            if row >= 0 and self._format_id_at(row) == self._selected_format_id:
                selector = self._selected_format_id

        self._persist_preferences(save_path, selector)
        self._submitted = True
        self.accept()

    def _persist_preferences(self, save_path: str, selector: str):
        cfg = self._config
        cfg.ytdlp_last_save_path = save_path
        cfg.ytdlp_last_format = selector
        cfg.ytdlp_embed_thumbnail = self._embed_thumb_cb.isChecked()
        cfg.ytdlp_embed_subtitles = self._embed_subs_cb.isChecked()
        cfg.ytdlp_subtitle_langs = self._subs_langs_edit.text().strip() or "en"
        try:
            cfg.save()
        except Exception as exc:
            log.debug("Could not persist YouTube preferences: %s", exc)

    # -- results -------------------------------------------------------------

    def selection(self) -> dict:
        """Return the chosen download request for the caller to dispatch."""
        if not self._submitted:
            return {}
        selector, max_height = self._current_preset()
        if not self._batch_mode and self._selected_format_id and max_height is not None and max_height > 0:
            row = self._table.currentRow()
            if row >= 0 and self._format_id_at(row) == self._selected_format_id:
                selector = self._selected_format_id

        if self._batch_mode and self._playlist_list.count() > 0:
            targets = self._selected_playlist_items()
        else:
            targets = [self._current] if self._current else []

        return {
            "videos": targets,
            "format_selector": selector,
            "save_path": normalize_path(self._save_edit.currentText().strip()),
        }

    def closeEvent(self, event):
        self._closing = True
        self._discard_worker()
        if self._thumb_reply is not None:
            try:
                self._thumb_reply.abort()
            except RuntimeError:
                pass
            self._thumb_reply = None
        super().closeEvent(event)
