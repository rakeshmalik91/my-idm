"""Antivirus & Security Settings dialog for My-IDM."""

from __future__ import annotations

import os
import tempfile
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from my_idm.security import (
    KNOWN_THREAT_CATEGORIES,
    SecurityConfig,
    find_windows_defender_path,
    scan_file,
)


class SecuritySettingsDialog(QDialog):
    """Dialog for configuring pre- and post-download antivirus scanning."""

    def __init__(self, current_config: SecurityConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Antivirus & Security Settings")
        self.setMinimumWidth(580)
        self.setModal(True)

        self._config = SecurityConfig.from_dict(current_config.to_dict())
        self._defender_path = find_windows_defender_path()

        self._setup_ui()
        self._populate_fields()

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
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        header = QLabel(
            "Configure real-time safety inspection before downloads start and automatic "
            "antivirus malware scanning once files are completed."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self._tabs = QTabWidget()
        tabs = self._tabs

        # Tab 1: Pre-Download Safety
        pre_tab = QWidget()
        pre_layout = QVBoxLayout(pre_tab)
        pre_layout.setSpacing(14)
        pre_layout.setContentsMargins(12, 16, 12, 12)

        self._scan_before_cb = QCheckBox("🔍 Enable URL and file safety check before downloading")
        self._scan_before_cb.toggled.connect(self._on_pre_scan_toggled)
        pre_layout.addWidget(self._scan_before_cb)

        url_group = QGroupBox("Inspection Rules")
        url_inner = QVBoxLayout(url_group)
        url_inner.setSpacing(10)

        self._warn_ext_cb = QCheckBox(
            "⚠️ Warn when downloading executable or script files (.exe, .msi, .bat, .vbs, .scr, .iso)"
        )
        url_inner.addWidget(self._warn_ext_cb)

        self._block_dangerous_cb = QCheckBox(
            "🚫 Automatically block high-risk URLs (e.g. deceptive double extensions like file.pdf.exe)"
        )
        url_inner.addWidget(self._block_dangerous_cb)
        pre_layout.addWidget(url_group)

        # VirusTotal group
        vt_group = QGroupBox("VirusTotal Online Reputation (Optional)")
        vt_inner = QVBoxLayout(vt_group)
        vt_desc = QLabel(
            "Optional: Enter your free VirusTotal API key to query 70+ online antivirus "
            "engines for URL safety verdicts before downloading."
        )
        vt_desc.setWordWrap(True)
        vt_desc.setStyleSheet("color: #a0aab8; font-size: 11px;")
        vt_inner.addWidget(vt_desc)

        vt_row = QHBoxLayout()
        vt_row.addWidget(QLabel("API Key:"))
        self._vt_key_edit = QLineEdit()
        self._vt_key_edit.setPlaceholderText("Paste your VirusTotal API key (optional)...")
        self._vt_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        vt_row.addWidget(self._vt_key_edit, 1)

        toggle_key_btn = QPushButton("👁")
        toggle_key_btn.setFixedWidth(32)
        toggle_key_btn.setCheckable(True)
        toggle_key_btn.toggled.connect(
            lambda chk: self._vt_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if chk else QLineEdit.EchoMode.Password
            )
        )
        vt_row.addWidget(toggle_key_btn)
        vt_inner.addLayout(vt_row)

        pre_layout.addWidget(vt_group)
        pre_layout.addStretch()
        tabs.addTab(self._wrap_scrollable(pre_tab), "🔍 Pre-Download Safety")

        # Tab 2: Post-Download Antivirus Scan
        post_tab = QWidget()
        post_layout = QVBoxLayout(post_tab)
        post_layout.setSpacing(14)
        post_layout.setContentsMargins(12, 16, 12, 12)

        self._scan_after_cb = QCheckBox("🛡️ Automatically scan files with antivirus after download")
        self._scan_after_cb.toggled.connect(self._on_post_scan_toggled)
        post_layout.addWidget(self._scan_after_cb)

        # Scanner engine group
        scanner_group = QGroupBox("Antivirus Scanner")
        scanner_inner = QVBoxLayout(scanner_group)
        scanner_inner.setSpacing(10)

        self._rb_defender = QRadioButton(
            f"Windows Defender ({'Found: ' + self._defender_path if self._defender_path else 'Not Detected'})"
        )
        self._rb_defender.toggled.connect(self._on_scanner_type_toggled)
        scanner_inner.addWidget(self._rb_defender)

        self._rb_custom = QRadioButton("Custom Antivirus Scanner Executable")
        self._rb_custom.toggled.connect(self._on_scanner_type_toggled)
        scanner_inner.addWidget(self._rb_custom)

        # Custom scanner details
        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("Executable:"))
        self._custom_path_edit = QLineEdit()
        self._custom_path_edit.setPlaceholderText("C:\\Program Files\\...\\scanner.exe")
        custom_row.addWidget(self._custom_path_edit, 1)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_custom_scanner)
        custom_row.addWidget(browse_btn)
        scanner_inner.addLayout(custom_row)

        args_row = QHBoxLayout()
        args_row.addWidget(QLabel("Arguments:"))
        self._custom_args_edit = QLineEdit()
        self._custom_args_edit.setPlaceholderText('"%file%"')
        args_row.addWidget(self._custom_args_edit, 1)
        scanner_inner.addLayout(args_row)

        args_hint = QLabel('Use "%file%" as the placeholder for the downloaded file path.')
        args_hint.setStyleSheet("color: #8892b0; font-size: 11px;")
        scanner_inner.addWidget(args_hint)

        post_layout.addWidget(scanner_group)

        # Scan Timing group
        timing_group = QGroupBox("Scan Timing")
        timing_inner = QVBoxLayout(timing_group)
        self._timing_group = QButtonGroup(self)

        self._rb_timing_auto = QRadioButton("🔄 Automatically scan when download completes")
        self._timing_group.addButton(self._rb_timing_auto)
        timing_inner.addWidget(self._rb_timing_auto)

        self._rb_timing_manual = QRadioButton("🖱️ Manual scan only (right-click → Scan with Antivirus)")
        self._timing_group.addButton(self._rb_timing_manual)
        timing_inner.addWidget(self._rb_timing_manual)

        post_layout.addWidget(timing_group)

        # Action on threat group
        action_group = QGroupBox("Action When Threat is Detected")
        action_inner = QVBoxLayout(action_group)
        self._action_group = QButtonGroup(self)

        self._rb_warn = QRadioButton("⚠️ Alert user and display threat warning (keep file)")
        self._action_group.addButton(self._rb_warn)
        action_inner.addWidget(self._rb_warn)

        self._rb_delete = QRadioButton(
            "🗑️ Alert user and automatically quarantine / delete infected file"
        )
        self._action_group.addButton(self._rb_delete)
        action_inner.addWidget(self._rb_delete)

        post_layout.addWidget(action_group)

        # Threat Exclusions group
        excl_group = QGroupBox("Threat Exclusions (silently allowed)")
        excl_inner = QVBoxLayout(excl_group)
        excl_inner.setSpacing(8)

        excl_desc = QLabel(
            "Threats matching any pattern or category in this list (e.g. HackTool, CrackTool, Keygen) "
            "will be silently allowed without triggering warnings or quarantine actions."
        )
        excl_desc.setWordWrap(True)
        excl_desc.setStyleSheet("color: #8892b0; font-size: 11px;")
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
        post_layout.addWidget(excl_group)

        post_layout.addStretch()
        tabs.addTab(self._wrap_scrollable(post_tab), "🛡️ Post-Download Antivirus")

        layout.addWidget(tabs)

        # Bottom row: Test Scanner button and Dialog buttons
        bottom_row = QHBoxLayout()
        test_btn = QPushButton("🧪 Test Antivirus Scanner")
        test_btn.clicked.connect(self._test_scanner)
        bottom_row.addWidget(test_btn)
        bottom_row.addStretch()

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_save)
        button_box.rejected.connect(self.reject)
        bottom_row.addWidget(button_box)

        layout.addLayout(bottom_row)

    def _populate_fields(self):
        self._scan_before_cb.setChecked(self._config.scan_before_download)
        self._warn_ext_cb.setChecked(self._config.warn_high_risk_extensions)
        self._block_dangerous_cb.setChecked(self._config.block_dangerous_urls)
        self._vt_key_edit.setText(self._config.virustotal_api_key)

        self._scan_after_cb.setChecked(self._config.scan_after_download)
        if self._config.scanner_type == "custom":
            self._rb_custom.setChecked(True)
        else:
            self._rb_defender.setChecked(True)

        self._custom_path_edit.setText(self._config.custom_scanner_path)
        self._custom_args_edit.setText(self._config.custom_scanner_args or '"%file%"')

        if self._config.action_on_threat == "delete":
            self._rb_delete.setChecked(True)
        else:
            self._rb_warn.setChecked(True)

        if self._config.scan_timing == "manual_only":
            self._rb_timing_manual.setChecked(True)
        else:
            self._rb_timing_auto.setChecked(True)

        self._threat_excl_list.clear()
        for cat in self._config.get_effective_threat_exclusions():
            if cat.strip():
                self._threat_excl_list.addItem(cat.strip())
        if self._config.ignored_threat_patterns:
            patterns = self._config.ignored_threat_patterns
            if isinstance(patterns, str):
                patterns = [p.strip() for p in patterns.split(",") if p.strip()]
            for pat in patterns:
                existing = [
                    self._threat_excl_list.item(i).text().strip().lower()
                    for i in range(self._threat_excl_list.count())
                ]
                if pat.lower() not in existing:
                    self._threat_excl_list.addItem(pat)

        self._on_pre_scan_toggled(self._config.scan_before_download)
        self._on_post_scan_toggled(self._config.scan_after_download)
        self._on_scanner_type_toggled()

    def _on_pre_scan_toggled(self, checked: bool):
        self._warn_ext_cb.setEnabled(checked)
        self._block_dangerous_cb.setEnabled(checked)
        self._vt_key_edit.setEnabled(checked)

    def _on_post_scan_toggled(self, checked: bool):
        self._rb_defender.setEnabled(checked)
        self._rb_custom.setEnabled(checked)
        self._rb_warn.setEnabled(checked)
        self._rb_delete.setEnabled(checked)
        self._on_scanner_type_toggled()

    def _on_scanner_type_toggled(self):
        is_custom = self._rb_custom.isChecked() and self._scan_after_cb.isChecked()
        self._custom_path_edit.setEnabled(is_custom)
        self._custom_args_edit.setEnabled(is_custom)

    def _browse_custom_scanner(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Antivirus Executable", "",
            "Executables (*.exe *.bat *.cmd);;All Files (*)",
        )
        if path:
            self._custom_path_edit.setText(path)

    def _test_scanner(self):
        """Run a test scan on a temporary clean file to verify the scanner works."""
        test_cfg = SecurityConfig(
            scan_after_download=True,
            scanner_type="custom" if self._rb_custom.isChecked() else "defender",
            custom_scanner_path=self._custom_path_edit.text().strip(),
            custom_scanner_args=self._custom_args_edit.text().strip(),
        )

        # Create temporary clean text file
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("My-IDM Antivirus Scanner Integration Test File.")
            temp_path = f.name

        try:
            is_clean, report = scan_file(temp_path, test_cfg)
            if is_clean:
                QMessageBox.information(
                    self, "Antivirus Scanner Test Passed",
                    f"Scanner executed successfully!\n\nVerdict: Clean\n\nReport:\n{report}"
                )
            else:
                QMessageBox.warning(
                    self, "Scanner Returned Warning/Alert",
                    f"Scanner execution completed with warning:\n\n{report}"
                )
        except Exception as exc:
            QMessageBox.critical(
                self, "Scanner Execution Failed",
                f"Could not execute antivirus scanner:\n\n{exc}"
            )
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

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

    def _on_save(self):
        self._config.scan_before_download = self._scan_before_cb.isChecked()
        self._config.warn_high_risk_extensions = self._warn_ext_cb.isChecked()
        self._config.block_dangerous_urls = self._block_dangerous_cb.isChecked()
        self._config.virustotal_api_key = self._vt_key_edit.text().strip()

        self._config.scan_after_download = self._scan_after_cb.isChecked()
        self._config.scanner_type = "custom" if self._rb_custom.isChecked() else "defender"
        self._config.custom_scanner_path = self._custom_path_edit.text().strip()
        self._config.custom_scanner_args = self._custom_args_edit.text().strip()
        self._config.action_on_threat = "delete" if self._rb_delete.isChecked() else "warn"
        self._config.scan_timing = "manual_only" if self._rb_timing_manual.isChecked() else "after_complete"
        excl_items = [
            self._threat_excl_list.item(i).text().strip()
            for i in range(self._threat_excl_list.count())
        ]
        self._config.ignored_threat_categories = [x for x in excl_items if x]
        self._config.ignored_threat_patterns = ""

        self.accept()

    @property
    def config(self) -> SecurityConfig:
        return self._config
