"""Dark theme stylesheet and color palette for My-IDM."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Asset paths
# ---------------------------------------------------------------------------

# Qt resolves stylesheet url() relative to the process working directory, so the
# tick used by list-item checkboxes is referenced by absolute path instead.
_CHECK_SVG_URL = ""
try:  # pragma: no cover - import guard only
    from my_idm.resources import RESOURCES_DIR

    _CHECK_SVG = RESOURCES_DIR / "check.svg"
    if _CHECK_SVG.is_file():
        _CHECK_SVG_URL = _CHECK_SVG.as_posix()
except Exception:  # pragma: no cover - fall back to the native tick
    _CHECK_SVG_URL = ""

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

class Colors:
    BG_DARK = "#0d1117"
    BG_MID = "#161b22"
    BG_LIGHT = "#21262d"
    BG_HOVER = "#30363d"
    BG_SELECTED = "rgba(46, 160, 67, 0.22)"

    BORDER = "#30363d"
    BORDER_LIGHT = "#484f58"

    TEXT = "#e6edf3"
    TEXT_SECONDARY = "#8b949e"
    TEXT_DIM = "#6e7681"
    TEXT_MUTED = "#6e7681"

    ACCENT = "#58a6ff"
    ACCENT_HOVER = "#79c0ff"
    GREEN = "#3fb950"
    ORANGE = "#d29922"
    RED = "#f85149"
    PURPLE = "#bc8cff"
    CYAN = "#39c5bb"

    # Progress bar colors
    PROGRESS_BG = "#21262d"
    PROGRESS_DOWNLOADING = "#1f6feb"
    PROGRESS_COMPLETE = "#238636"
    PROGRESS_PAUSED = "#9e6a03"
    PROGRESS_ERROR = "#da3633"
    PROGRESS_SEEDING = "#8957e5"


# ---------------------------------------------------------------------------
# Qt Stylesheet (QSS)
# ---------------------------------------------------------------------------

# Injected into the indicator rules. Empty when the SVG is unavailable, which
# leaves the tick unstyled rather than breaking the whole stylesheet.
_check_image = f"image: url({_CHECK_SVG_URL});" if _CHECK_SVG_URL else ""

DARK_STYLESHEET = f"""
/* ---- Global ---- */
QWidget {{
    background-color: {Colors.BG_DARK};
    color: {Colors.TEXT};
    font-family: "Segoe UI", "Inter", "Roboto", sans-serif;
    font-size: 13px;
}}

/* ---- Main Window ---- */
QMainWindow {{
    background-color: {Colors.BG_DARK};
}}

QMainWindow::separator {{
    background: {Colors.BORDER};
    width: 1px;
    height: 1px;
}}

/* ---- Menu Bar ---- */
QMenuBar {{
    background-color: {Colors.BG_MID};
    border-bottom: 1px solid {Colors.BORDER};
    padding: 2px 4px;
}}

QMenuBar::item {{
    padding: 4px 10px;
    border-radius: 4px;
}}

QMenuBar::item:selected {{
    background-color: {Colors.BG_HOVER};
}}

QMenu {{
    background-color: {Colors.BG_MID};
    border: 1px solid {Colors.BORDER};
    border-radius: 6px;
    padding: 4px 0;
}}

QMenu::item {{
    padding: 6px 24px 6px 8px;
}}

QMenu::icon {{
    padding-left: 6px;
}}

QMenu::item:selected {{
    background-color: {Colors.BG_HOVER};
}}

QMenu::separator {{
    height: 1px;
    background: {Colors.BORDER};
    margin: 4px 8px;
}}

/* ---- Toolbar ---- */
QToolBar {{
    background-color: {Colors.BG_MID};
    border-bottom: 1px solid {Colors.BORDER};
    padding: 4px 8px;
    spacing: 4px;
}}

QToolBar > QWidget {{
    background-color: transparent;
}}

QToolBar::separator {{
    width: 1px;
    background: {Colors.BORDER};
    margin: 4px 6px;
}}

/* Toolbar quick-search: sits in the gap before the Preferences button. */
QLineEdit#toolbar_search {{
    background-color: {Colors.BG_DARK};
    border: 1px solid {Colors.BORDER};
    border-radius: 13px;
    padding: 2px 12px;
    color: {Colors.TEXT};
    selection-background-color: {Colors.ACCENT};
    selection-color: {Colors.TEXT};
}}

QLineEdit#toolbar_search:focus {{
    border-color: {Colors.ACCENT};
}}

QLineEdit#toolbar_search::placeholder {{
    color: {Colors.TEXT_DIM};
}}

QToolButton {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 10px;
    color: {Colors.TEXT};
    font-weight: 500;
}}

QToolButton:hover {{
    background-color: {Colors.BG_HOVER};
    border-color: {Colors.BORDER};
}}

QToolButton:pressed {{
    background-color: {Colors.BG_LIGHT};
}}

QToolButton:checked {{
    background-color: {Colors.BG_LIGHT};
    border-color: {Colors.ACCENT};
    color: {Colors.ACCENT};
}}

QToolButton:checked:hover {{
    background-color: {Colors.BG_HOVER};
    border-color: {Colors.ACCENT_HOVER};
    color: {Colors.ACCENT_HOVER};
}}

QToolButton:disabled {{
    color: {Colors.TEXT_DIM};
}}

/* ---- Table View ---- */
QTableView {{
    background-color: {Colors.BG_DARK};
    alternate-background-color: {Colors.BG_MID};
    border: none;
    gridline-color: {Colors.BORDER};
    selection-background-color: {Colors.BG_SELECTED};
    selection-color: {Colors.TEXT};
}}

QTableView::item {{
    padding: 4px 8px;
    border-bottom: 1px solid {Colors.BORDER};
}}

QTableView::item:selected {{
    background-color: {Colors.BG_SELECTED};
}}

QTableView::item:hover {{
    background-color: {Colors.BG_HOVER};
}}

QHeaderView {{
    background-color: {Colors.BG_MID};
}}

QHeaderView::section {{
    background-color: {Colors.BG_MID};
    color: {Colors.TEXT_SECONDARY};
    border: none;
    border-bottom: 2px solid {Colors.BORDER};
    border-right: 1px solid {Colors.BORDER};
    padding: 6px 22px 6px 8px;
    font-weight: 600;
    font-size: 12px;
    text-transform: uppercase;
}}

QHeaderView::section:hover {{
    color: {Colors.TEXT};
    background-color: {Colors.BG_LIGHT};
}}

/* ---- Scrollbars ---- */
QScrollBar:vertical {{
    background: {Colors.BG_DARK};
    width: 10px;
    margin: 0;
    border: none;
}}

QScrollBar::handle:vertical {{
    background: {Colors.BG_HOVER};
    border-radius: 5px;
    min-height: 30px;
}}

QScrollBar::handle:vertical:hover {{
    background: {Colors.BORDER_LIGHT};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: {Colors.BG_DARK};
    height: 10px;
    margin: 0;
    border: none;
}}

QScrollBar::handle:horizontal {{
    background: {Colors.BG_HOVER};
    border-radius: 5px;
    min-width: 30px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {Colors.BORDER_LIGHT};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ---- Status Bar ---- */
QStatusBar {{
    background-color: {Colors.BG_MID};
    border-top: 1px solid {Colors.BORDER};
    color: {Colors.TEXT_SECONDARY};
    font-size: 12px;
    padding: 2px 8px;
}}

/* ---- Dialogs ---- */
QDialog {{
    background-color: {Colors.BG_MID};
    border: 1px solid {Colors.BORDER};
}}

QLabel {{
    color: {Colors.TEXT};
    background: transparent;
}}

QLineEdit {{
    background-color: {Colors.BG_DARK};
    border: 1px solid {Colors.BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    color: {Colors.TEXT};
    selection-background-color: {Colors.ACCENT};
}}

QLineEdit:focus {{
    border-color: {Colors.ACCENT};
}}

/* Editable combos embed a QLineEdit; without an outer border the control reads
   as a borderless text field, unlike every other input. Scope the rule to
   editable combos so the plain drop-downs keep their existing appearance. */
QComboBox[editable="true"] {{
    background-color: {Colors.BG_DARK};
    border: 1px solid {Colors.BORDER};
    border-radius: 6px;
    padding: 4px 6px;
    color: {Colors.TEXT};
}}

QComboBox[editable="true"]:focus {{
    border-color: {Colors.ACCENT};
}}

QComboBox[editable="true"] QLineEdit {{
    background: transparent;
    border: none;
    padding: 2px 4px;
    color: {Colors.TEXT};
    selection-background-color: {Colors.ACCENT};
}}

QSpinBox {{
    background-color: {Colors.BG_DARK};
    border: 1px solid {Colors.BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    color: {Colors.TEXT};
}}

QSpinBox:focus {{
    border-color: {Colors.ACCENT};
}}

QPushButton {{
    background-color: {Colors.BG_LIGHT};
    border: 1px solid {Colors.BORDER};
    border-radius: 6px;
    padding: 6px 16px;
    color: {Colors.TEXT};
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: {Colors.BG_HOVER};
    border-color: {Colors.BORDER_LIGHT};
}}

QPushButton:pressed {{
    background-color: {Colors.BG_DARK};
}}

QPushButton:disabled {{
    color: {Colors.TEXT_DIM};
    background-color: {Colors.BG_DARK};
}}

QPushButton#primaryButton {{
    background-color: {Colors.ACCENT};
    border-color: {Colors.ACCENT};
    color: #ffffff;
}}

QPushButton#primaryButton:hover {{
    background-color: {Colors.ACCENT_HOVER};
}}

QPushButton#dangerButton {{
    background-color: {Colors.RED};
    border-color: {Colors.RED};
    color: #ffffff;
}}

QPushButton#detailsCloseBtn {{
    padding: 0px;
    font-size: 14px;
    font-weight: bold;
    color: {Colors.TEXT_SECONDARY};
}}

QPushButton#detailsCloseBtn:hover {{
    color: #ffffff;
    background-color: #f8514933;
    border-color: {Colors.RED};
}}

QCheckBox {{
    spacing: 8px;
    color: {Colors.TEXT};
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {Colors.BORDER};
    border-radius: 3px;
    background: {Colors.BG_DARK};
}}

QCheckBox::indicator:checked {{
    background: {Colors.ACCENT};
    border-color: {Colors.ACCENT};
}}

/* Item-view checkboxes are drawn through the *view's* ::indicator sub-control,
   not QCheckBox::indicator, so they fell back to a dark default whose border is
   invisible on these backgrounds. Only the outline is declared here: the tick
   itself stays the native one, so these still read as "✓ in a box" rather than
   the solid accent fill used by QCheckBox. */
QListWidget::indicator,
QListView::indicator,
QTreeWidget::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {Colors.BORDER};
    border-radius: 3px;
    background: {Colors.BG_DARK};
}}

QListWidget::indicator:hover,
QListView::indicator:hover,
QTreeWidget::indicator:hover {{
    border-color: {Colors.ACCENT};
}}

/* Styling ::indicator at all suppresses Qt's native check primitive, so the tick
   has to be supplied explicitly. Only the outline and the tick change: the fill
   stays dark, which keeps these reading as "✓ in a box" rather than the solid
   accent block used by QCheckBox. */
QListWidget::indicator:checked,
QListView::indicator:checked,
QTreeWidget::indicator:checked {{
    border: 1px solid {Colors.ACCENT};
    border-radius: 3px;
    background: {Colors.BG_DARK};
    {_check_image}
}}

/* ---- Tooltips ---- */
QToolTip {{
    background-color: {Colors.BG_LIGHT};
    border: 1px solid {Colors.BORDER};
    color: {Colors.TEXT};
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 12px;
}}

/* ---- Group Box ---- */
QGroupBox {{
    border: 1px solid {Colors.BORDER};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 16px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: {Colors.TEXT_SECONDARY};
}}

/* ---- Tab Widget ---- */
QTabWidget::pane {{
    border: 1px solid {Colors.BORDER};
    background-color: {Colors.BG_MID};
    border-radius: 4px;
}}

QTabBar::tab {{
    background-color: {Colors.BG_DARK};
    color: {Colors.TEXT_SECONDARY};
    border: 1px solid {Colors.BORDER};
    border-bottom: none;
    padding: 6px 14px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    font-weight: 500;
}}

QTabBar::tab:selected {{
    background-color: {Colors.BG_MID};
    color: {Colors.ACCENT};
    border-bottom: 2px solid {Colors.ACCENT};
}}

QTabBar::tab:hover:!selected {{
    background-color: {Colors.BG_HOVER};
    color: {Colors.TEXT};
}}

/* ---- Splitter ---- */
QSplitter::handle {{
    background-color: {Colors.BORDER};
}}

QSplitter::handle:vertical {{
    height: 4px;
}}

QSplitter::handle:horizontal {{
    width: 4px;
}}

QSplitter::handle:hover {{
    background-color: {Colors.ACCENT};
}}
"""
