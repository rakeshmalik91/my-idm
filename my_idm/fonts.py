"""Platform-appropriate font families, in one place.

Sixteen call sites were hardcoding `"Segoe UI"` or `"Consolas"`. On Windows that is right; on Linux
and macOS Qt falls back to whatever it likes, which is usually a default sans that ignores the
app's own metrics, and for the two monospace sites it means the console and the file-tree columns
lose their fixed-pitch alignment - a monospace font substituted with a proportional one turns a
column of numbers into a ragged edge.

The lists below are ordered preference chains, not single names, because no one family is available
everywhere. Qt resolves a list itself: ``QFont(["Noto Sans", "DejaVu Sans"])`` picks the first
family the font database actually has, so the caller never has to check.

Every list starts with the platform's own UI font, because the OS default is what the user's other
applications use, and matching it is what makes an app feel native rather than merely functional.
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional

from PySide6.QtGui import QFont, QFontDatabase

log = logging.getLogger(__name__)

# Proportional (UI) families, most-preferred first.
_UI_FAMILIES = {
    "win32": ["Segoe UI", "Inter", "Roboto", "Noto Sans", "DejaVu Sans", "sans-serif"],
    "darwin": [
        # .AppleSystemUIFont is the private name for the system face; SF Pro Text is its public
        # equivalent and is what a Qt build outside AppKit is more likely to have registered.
        ".AppleSystemUIFont",
        "SF Pro Text",
        "Helvetica Neue",
        "Helvetica",
        "Inter",
        "Noto Sans",
        "sans-serif",
    ],
    "default": [
        # Linux and anything else POSIX. The first three are the common distribution defaults;
        # DejaVu is on essentially every system as a last resort before Qt's own fallback.
        "Inter",
        "Noto Sans",
        "Ubuntu",
        "Cantarell",
        "DejaVu Sans",
        "Liberation Sans",
        "sans-serif",
    ],
}

_MONO_FAMILIES = {
    "win32": ["Cascadia Mono", "Consolas", "Cascadia Code", "Lucida Console", "Courier New"],
    "darwin": ["SF Mono", "Menlo", "Monaco", "Andale Mono", "Courier New"],
    "default": [
        "JetBrains Mono",
        "Fira Code",
        "Ubuntu Mono",
        "DejaVu Sans Mono",
        "Liberation Mono",
        "Courier New",
    ],
}

#: Emoji needs a colour font. The generic names are last because Qt resolves a missing family to
#: the default and prints a warning per lookup, whereas "sans-serif" always resolves.
_EMOJI_FAMILIES = [
    "Segoe UI Emoji",
    "Apple Color Emoji",
    "Noto Color Emoji",
    "Twemoji Mozilla",
    "Segoe UI Symbol",
    "sans-serif",
]

#: Fallback for a platform key we have no list for. Kept non-empty deliberately: an empty family
#: list makes QFont pick a default *silently*, which is the failure this module exists to prevent.
_GENERIC_SANS = ["Noto Sans", "DejaVu Sans", "sans-serif"]


def _platform_key() -> str:
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "default"


def ui_font_families() -> List[str]:
    """Proportional families for application UI, most-preferred first."""
    return list(_UI_FAMILIES[_platform_key()])


def mono_font_families() -> List[str]:
    """Fixed-pitch families, for the console and file-tree columns.

    Monospace is not cosmetic here. `shorten_path` and the segment table measure text to decide
    where to elide, and a proportional substitution makes those measurements wrong.
    """
    return list(_MONO_FAMILIES[_platform_key()]) or list(_GENERIC_SANS)


def emoji_font_families() -> List[str]:
    """Colour-emoji families.

    Platform-independent and identical on all three: this list is already correct, it was just
    duplicated in two modules.
    """
    return list(_EMOJI_FAMILIES)


def available_family(families: List[str]) -> Optional[str]:
    """The first family in ``families`` the font database actually has.

    Returns ``None`` when nothing matches. Callers use this only to *log* - the QFont lists resolve
    on their own, and a resolver that second-guessed them would be a second source of truth to keep
    in sync.
    """
    try:
        available = set(QFontDatabase.families())
    except Exception as exc:  # no QGuiApplication yet, or no font database
        log.debug("Font database unavailable: %s", exc)
        return None
    for family in families:
        if family in available:
            return family
    return None


def ui_font(point_size: int = 10, bold: bool = False) -> QFont:
    """A UI font for the platform.

    The `or _GENERIC_SANS` is the real safeguard, not defensiveness: an empty family list does not
    raise, Qt silently substitutes its own default, and the result looks wrong in a way nothing in
    the log would explain. If someone empties the tables above, this keeps the app legible.
    """
    font = QFont(ui_font_families() or _GENERIC_SANS, point_size)
    if bold:
        font.setWeight(QFont.Weight.Bold)
    return font


def mono_font(point_size: int = 10, bold: bool = False) -> QFont:
    """A fixed-pitch font for the platform."""
    font = QFont(mono_font_families() or ["monospace"], point_size)
    if bold:
        font.setWeight(QFont.Weight.Bold)
    return font


def emoji_font(point_size: int = 12) -> QFont:
    """A colour-emoji font."""
    return QFont(emoji_font_families() or ["sans-serif"], point_size)


def stylesheet_family() -> str:
    """The proportional chain as a CSS ``font-family`` value.

    Qt's stylesheet parser accepts a comma-separated fallback list and picks the first family it
    has, so this is the same chain as :func:`ui_font_families` in the form the stylesheet needs.
    Quoted, because a family name containing a space or a digit is not a valid unquoted CSS ident.
    """
    return ", ".join(f'"{family}"' for family in ui_font_families())
