"""Theme stylesheets and colour palettes for My-IDM.

Two themes are available, ``dark`` (the default) and ``light``. ``Colors`` is the *live*
palette: ``apply_theme`` points it at one of :data:`THEMES` and every module reads
``Colors.X`` when it builds a stylesheet, so a theme change reaches both the global sheet
and the per-widget ones.
"""

from __future__ import annotations

import re

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
    # Dimmer than TEXT_DIM on purpose: this is for controls that are *unavailable*, not merely
    # de-emphasised, and it has to read as unavailable at a glance against BG_MID.
    TEXT_DISABLED = "#4d5560"

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
# Themes
# ---------------------------------------------------------------------------

#: The light palette. Same attribute names as ``Colors`` - that is the whole contract.
#: ``Colors`` is the *active* palette: ``apply_theme`` copies one of these onto it, and
#: every module in the codebase already reads ``Colors.X`` at the moment it builds an
#: inline stylesheet (``details_panel``, ``settings_dialog``, ``stats_dialog`` and about
#: forty more). Making ``Colors`` swappable is therefore what lets a theme change reach
#: those per-widget sheets without touching any of them.
#:
#: Values are the light half of the same GitHub palette the dark theme uses, so the two
#: read as one design rather than as an afterthought.
class LightColors:
    BG_DARK = "#ffffff"
    BG_MID = "#f6f8fa"
    BG_LIGHT = "#eaeef2"
    BG_HOVER = "#d0d7de"
    BG_SELECTED = "rgba(9, 105, 218, 0.12)"
    BORDER = "#d0d7de"
    BORDER_LIGHT = "#afb8c1"

    TEXT = "#1f2328"
    TEXT_SECONDARY = "#59636e"
    TEXT_DIM = "#818b98"
    TEXT_MUTED = "#818b98"
    TEXT_DISABLED = "#a8b0ba"

    ACCENT = "#0969da"
    ACCENT_HOVER = "#0550ae"
    GREEN = "#1a7f37"
    ORANGE = "#9a6700"
    RED = "#cf222e"
    PURPLE = "#8250df"
    CYAN = "#1b7c83"

    PROGRESS_BG = "#eaeef2"
    PROGRESS_DOWNLOADING = "#0969da"
    PROGRESS_COMPLETE = "#1a7f37"
    PROGRESS_PAUSED = "#9a6700"
    PROGRESS_ERROR = "#cf222e"
    PROGRESS_SEEDING = "#8250df"

    # Mirrors the colours ``Colors`` declares, so the same sheet renders sensibly light.
    # Only the global sheet is themed: roughly 190 hex values are still hard-coded in the
    # per-widget inline stylesheets across nine modules, so a few widgets keep their dark
    # fills under this theme. Converting them is a separate job - see the note on
    # ``apply_theme`` - and was deliberately not bundled with the theme selector, because
    # collapsing those near-duplicate shades changed the established dark theme.
    BG_DARK = "#ffffff"
    BG_MID = "#f6f8fa"
    BG_LIGHT = "#eaeef2"
    BG_HOVER = "#d0d7de"
    BG_SELECTED = "rgba(9, 105, 218, 0.12)"
    BORDER = "#d0d7de"
    BORDER_LIGHT = "#afb8c1"

    TEXT = "#1f2328"
    TEXT_SECONDARY = "#59636e"
    TEXT_DIM = "#818b98"
    TEXT_MUTED = "#818b98"
    TEXT_DISABLED = "#a8b0ba"

    ACCENT = "#0969da"
    ACCENT_HOVER = "#0550ae"
    GREEN = "#1a7f37"
    ORANGE = "#9a6700"
    RED = "#cf222e"
    PURPLE = "#8250df"
    CYAN = "#1b7c83"

    PROGRESS_BG = "#eaeef2"
    PROGRESS_DOWNLOADING = "#0969da"
    PROGRESS_COMPLETE = "#1a7f37"
    PROGRESS_PAUSED = "#9a6700"
    PROGRESS_ERROR = "#cf222e"
    PROGRESS_SEEDING = "#8250df"


#: Theme name -> palette class. ``"dark"`` maps to ``Colors`` itself, which is already
#: dark, so the default needs no swap.
THEMES: dict[str, type] = {
    "dark": Colors,
    "light": LightColors,
}

#: Theme id -> the palette's colours, captured once at import.
#:
#: This has to be a *frozen copy* and not a lookup into `THEMES`, because `Colors` is the
#: live palette: `apply_theme` mutates its attributes, so after switching to light both
#: entries would describe light and `current_theme()` would always answer "dark".
_PALETTES: dict[str, dict[str, str]] = {
    name: {
        key: value
        for key, value in vars(palette).items()
        if key.isupper() and isinstance(value, str)
    }
    for name, palette in THEMES.items()
}

#: Shown in the Preferences ▸ Views combo, in this order.
THEME_NAMES: tuple[str, ...] = ("dark", "light")

#: Theme id -> the label the selector shows. Kept out of the palette classes because a
#: palette is only colours; a label is presentation.
THEME_LABELS: dict[str, str] = {
    "dark": "🌙 Dark",
    "light": "☀️ Light",
}

DEFAULT_THEME = "dark"


def normalize_theme(name: object) -> str:
    """Coerce *name* to a known theme id, falling back to :data:`DEFAULT_THEME`.

    Values reach this from ``ui_state``, which is free text a hand-edited database or an
    older build can hold anything in, so an unknown id must not raise.
    """
    text = str(name or "").strip().lower()
    return text if text in THEMES else DEFAULT_THEME


_COLORS_REF = re.compile(r"Colors\.([A-Z][A-Z_0-9]*)")


def themed(template: str) -> str:
    r"""Substitute the literal words ``Colors.NAME`` in *template* with their values.

    For the per-widget stylesheets that must follow a theme switch.

    Two things make this necessary. A QSS rule body is ``SideTabBar { ... }``, and in an
    f-string those braces are replacement fields - the string cannot be an f-string at all
    without escaping every brace, which makes the stylesheet unreadable. And a stylesheet
    built with an f-string is *resolved once*, at construction, so it keeps the palette it
    was born with: a panel built while the light theme was live stays white after a switch
    back to dark.

    Storing the literal ``Colors.NAME`` spelling and substituting on demand fixes both - the
    template needs no escaping, and :func:`apply_theme` can re-resolve it later.
    """
    return _COLORS_REF.sub(lambda m: str(getattr(Colors, m.group(1), "")), template)


def _snapshot(palette: type) -> dict[str, str]:
    return {
        key: value
        for key, value in vars(palette).items()
        if key.isupper() and isinstance(value, str)
    }


def _restore(saved: dict[str, str]) -> None:
    for key, value in saved.items():
        setattr(Colors, key, value)


def current_theme() -> str:
    """The theme id ``Colors`` currently holds."""
    active = _snapshot(Colors)
    for name, palette in _PALETTES.items():
        if palette == active:
            return name
    return DEFAULT_THEME


def apply_theme(app, name: object) -> str:
    """Point ``Colors`` and the application stylesheet at *name*, live.

    Returns the normalised theme id actually applied.

    Two things have to change together, and forgetting either is why a half-themed window
    looks worse than either extreme:

    * ``Colors`` - so the inline per-widget stylesheets built on demand (status labels,
      button sheets, delegate colours) use the new palette. Widgets already built keep the
      colours they were given, which is why they are re-polished below.
    * ``app.setStyleSheet`` - the global sheet.

    ``app.setStyleSheet`` only re-styles *new* widgets, so every existing one is unpolished
    and polished again. Without that the main window keeps its old colours while any dialog
    opened afterwards comes out in the new ones.
    """
    theme = normalize_theme(name)
    if theme == current_theme():
        # Nothing to do, and this is the common case: `MainWindow.__init__` applies the
        # persisted theme, which is dark unless the user chose otherwise. Setting the sheet
        # restyles the whole application, so doing it unconditionally would make every
        # Preferences save a full restyle.
        return theme
    # From the frozen snapshot, not `_snapshot(THEMES[theme])`: `Colors` *is* the dark
    # palette, so reading it back would hand "dark" whatever colours are live right now -
    # asking for dark while light is applied would restore light and silently do nothing.
    _restore(_PALETTES[theme])
    # `setStyleSheet` is enough to re-theme widgets that already exist - measured, not
    # assumed: the toolbar's fill changes with no explicit unpolish/polish.
    app.setStyleSheet(STYLESHEETS[theme])
    _retheme_widgets(app)
    return theme


def _retheme_widgets(app) -> None:
    """Re-resolve every widget whose stylesheet was stored as a template by :func:`themed_widget`.

    ``setStyleSheet`` cannot do this on its own. A widget given a *resolved* stylesheet has
    no idea which of its colours came from the palette, so it has nothing to update - which
    is why the details-panel sidebar stayed white after a switch from light back to dark.
    Storing the template as a widget property is what makes the change reversible: without
    it the original spelling is gone the moment ``themed()`` substitutes, and there is
    nothing left to re-resolve.

    Only widgets that opted in by going through :func:`themed_widget` are touched. This runs
    on an actual theme change only, never on a Preferences save that left the theme alone.
    """
    for widget in app.allWidgets():
        try:
            template = widget.property(THEME_TEMPLATE_PROPERTY)
            if template:
                widget.setStyleSheet(themed(template))
        except RuntimeError:
            # A deleted C++ object still reachable from the widget list. Not worth failing a
            # theme switch over.
            continue


#: Widget property under which :func:`themed_widget` stashes the stylesheet template.
THEME_TEMPLATE_PROPERTY = "myidmThemeTemplate"


def themed_widget(widget, template: str):
    """Apply *template* to *widget* and remember it, so a theme switch can re-resolve it.

    Use this rather than ``widget.setStyleSheet(themed(...))`` for any stylesheet that should
    follow the theme. Plain :func:`themed` resolves once and is therefore correct only for a
    sheet built at construction while the right theme is already live.
    """
    widget.setProperty(THEME_TEMPLATE_PROPERTY, template)
    widget.setStyleSheet(themed(template))
    return widget


def _build_stylesheet(palette: type) -> str:
    """Render the QSS for *palette*.

    The sheet is one f-string referring to ``Colors.X`` a few hundred times, and
    ``Colors`` is the *live* palette, so the sheet can only be rendered by temporarily
    binding it. Scoped to this function with a ``finally``: the alternative is rewriting
    every reference into a lookup, which is a much larger diff for no behavioural gain.
    """
    saved = _snapshot(Colors)
    _restore(_snapshot(palette))
    try:
        return _stylesheet_for_palette()
    finally:
        _restore(saved)


# ---------------------------------------------------------------------------
# Qt Stylesheet (QSS)
# ---------------------------------------------------------------------------

# Injected into the indicator rules. Empty when the SVG is unavailable, which
# leaves the tick unstyled rather than breaking the whole stylesheet.
_check_image = f"image: url({_CHECK_SVG_URL});" if _CHECK_SVG_URL else ""

#: The QSS body, rendered per theme by :func:`_build_stylesheet`. Named so the raw
#: template is obviously not the stylesheet anyone should apply - use
#: :func:`apply_theme`, or ``STYLESHEETS[...]`` for a specific id.
def _stylesheet_for_palette() -> str:
    """The QSS for whichever palette ``Colors`` holds right now.

    An f-string rather than a module constant, because the sheet interpolates
    ``Colors`` directly and therefore has to be *evaluated* per theme, not stored.
    """
    return f"""
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

    /* An unavailable item must read as unavailable. Without an explicit rule this fell back to
       Qt's own disabled rendering, which on this palette is barely dimmer than the enabled
       text - so a greyed-out "Move to Queue" looked live and read as "not clickable". */
    QMenu::item:disabled {{
        color: {Colors.TEXT_DISABLED};
        background-color: transparent;
    }}

    QMenu::item:disabled:selected {{
        background-color: transparent;
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

    /* ---- Item Views (Lists, Trees) ---- */
    QListWidget::item,
    QListView::item,
    QTreeWidget::item {{
        padding: 3px 4px;
        min-height: 20px;
    }}

    /* Item-view checkboxes are drawn through the *view's* ::indicator sub-control,
       not QCheckBox::indicator, so they fell back to a dark default whose border is
       invisible on these backgrounds. Sized to 14px (16px outer with border) so they
       fit cleanly within item row heights without cropping. */
    QListWidget::indicator,
    QListView::indicator,
    QTreeWidget::indicator {{
        width: 14px;
        height: 14px;
        border: 1px solid {Colors.BORDER};
        border-radius: 3px;
        background: {Colors.BG_DARK};
        margin-right: 6px;
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
        /* Mask the frame behind the text. A QGroupBox title conventionally *sits on* the
           top border with the border broken behind it, which only works if the title is
           opaque: with no background the 1px border shows through the glyphs and the label
           reads as struck through. There is no per-widget background here, so the box colour
           is inherited from the `QWidget` rule. */
        background: {Colors.BG_DARK};
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

#: Theme id -> rendered stylesheet. Build both up front so switching is a dict lookup, and
#: so ``apply_theme`` never re-renders 500 lines of QSS on the GUI thread.
STYLESHEETS: dict[str, str] = {
    name: _build_stylesheet(palette) for name, palette in THEMES.items()
}

#: Retained for the many callers that hard-code the dark theme at startup, and because it
#: is the documented default. ``apply_theme`` is the way to change it.
DARK_STYLESHEET = STYLESHEETS[DEFAULT_THEME]

_restore(_snapshot(THEMES[DEFAULT_THEME]))

# Keep the frozen copies in step with whatever the palette classes say now, so a future
# edit to `Colors` or `LightColors` cannot leave `current_theme()` reporting a theme that
# no longer matches. Cheap, and it runs once at import.
_PALETTES["dark"] = _snapshot(Colors)
