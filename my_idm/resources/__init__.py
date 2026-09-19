"""Application resources and logo helpers."""

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap

RESOURCES_DIR = Path(__file__).resolve().parent
LOGO_PNG = RESOURCES_DIR / "logo.png"
LOGO_ICO = RESOURCES_DIR / "logo.ico"


def get_app_icon() -> QIcon:
    """Return application QIcon with multi-resolution support."""
    if LOGO_ICO.exists():
        icon = QIcon(str(LOGO_ICO))
        if not icon.isNull():
            return icon
    if LOGO_PNG.exists():
        return QIcon(str(LOGO_PNG))
    return QIcon()


def get_app_logo_pixmap(size: int = 64) -> QPixmap:
    """Return scaled logo QPixmap for dialogs, about screens, and headers."""
    if LOGO_PNG.exists():
        pm = QPixmap(str(LOGO_PNG))
        if not pm.isNull():
            return pm.scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    return QPixmap()
