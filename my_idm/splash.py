"""Modern splash screen for My-IDM application startup."""

from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QSplashScreen, QWidget

from my_idm.resources import LOGO_PNG, get_app_logo_pixmap
from my_idm.styles import Colors
from my_idm import fonts

SPLASH_WIDTH = 500
SPLASH_HEIGHT = 270
APP_VERSION = "v1.0.0"


class IDMSplashScreen(QSplashScreen):
    """Custom frameless splash screen displaying logo, version, and loading status."""

    def __init__(self, parent: QWidget | None = None):
        # Create an initial transparent canvas for the splash screen window
        canvas = QPixmap(SPLASH_WIDTH, SPLASH_HEIGHT)
        canvas.fill(Qt.GlobalColor.transparent)
        super().__init__(canvas)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        self._message: str = "Starting My-IDM..."
        self._progress: int = 0  # 0 to 100
        self._logo_pixmap: QPixmap = get_app_logo_pixmap(64)
        if self._logo_pixmap.isNull() and LOGO_PNG.exists():
            pm = QPixmap(str(LOGO_PNG))
            if not pm.isNull():
                self._logo_pixmap = pm.scaled(
                    64, 64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )

    @property
    def message(self) -> str:
        return self._message

    @property
    def progress(self) -> int:
        return self._progress

    def set_message(self, message: str, progress: int | None = None):
        """Update current startup step description and optional progress (0-100)."""
        self._message = message
        if progress is not None:
            self._progress = max(0, min(100, int(progress)))
        self.repaint()
        app = QApplication.instance()
        if app:
            app.processEvents()

    def set_progress(self, progress: int):
        """Update current progress percentage without changing message."""
        self._progress = max(0, min(100, int(progress)))
        self.repaint()
        app = QApplication.instance()
        if app:
            app.processEvents()

    def paintEvent(self, event):
        """Render high-DPI modern splash screen with gradient, typography, and progress."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w = float(self.width())
        h = float(self.height())
        radius = 16.0

        # 1. Background card with subtle drop-shadow/border
        bg_rect = QRectF(1.0, 1.0, w - 2.0, h - 2.0)
        bg_gradient = QLinearGradient(0, 0, 0, h)
        bg_gradient.setColorAt(0.0, QColor("#161b22"))
        bg_gradient.setColorAt(0.6, QColor("#0f1319"))
        bg_gradient.setColorAt(1.0, QColor("#0d1117"))

        painter.setBrush(bg_gradient)
        painter.setPen(QColor(Colors.BORDER))
        painter.drawRoundedRect(bg_rect, radius, radius)

        # 2. Header subtle accent line at top border
        top_accent_rect = QRectF(36.0, 1.0, w - 72.0, 2.0)
        accent_grad = QLinearGradient(36.0, 0, w - 36.0, 0)
        accent_grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        accent_grad.setColorAt(0.5, QColor(Colors.ACCENT))
        accent_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(top_accent_rect, accent_grad)

        # 3. Logo
        logo_x = 36.0
        logo_y = 38.0
        logo_size = 64.0
        if not self._logo_pixmap.isNull():
            # Draw subtle glow behind logo
            glow_rect = QRectF(logo_x - 3, logo_y - 3, logo_size + 6, logo_size + 6)
            glow_path = QPainterPath()
            glow_path.addRoundedRect(glow_rect, 14, 14)
            painter.fillPath(glow_path, QColor(88, 166, 255, 20))

            # Draw rounded logo to avoid sharp white corners
            clip_path = QPainterPath()
            clip_path.addRoundedRect(QRectF(logo_x, logo_y, logo_size, logo_size), 12, 12)
            painter.save()
            painter.setClipPath(clip_path)
            painter.drawPixmap(
                int(logo_x), int(logo_y), int(logo_size), int(logo_size),
                self._logo_pixmap
            )
            painter.restore()

        # 4. App Name & Version Pill
        text_x = 118.0
        title_y = 66.0

        # Title: "My-IDM"
        painter.setPen(QColor(Colors.TEXT))
        title_font = fonts.ui_font(22, bold=True)
        painter.setFont(title_font)
        painter.drawText(QPointF(text_x, title_y), "My-IDM")

        fm = painter.fontMetrics()
        title_width = fm.horizontalAdvance("My-IDM")

        # Version Pill Badge: "v1.0.0"
        pill_x = text_x + title_width + 12.0
        pill_y = title_y - 19.0
        pill_rect = QRectF(pill_x, pill_y, 54.0, 22.0)
        pill_path = QPainterPath()
        pill_path.addRoundedRect(pill_rect, 11, 11)
        painter.fillPath(pill_path, QColor(88, 166, 255, 30))
        painter.setPen(QColor(88, 166, 255, 120))
        painter.drawPath(pill_path)

        pill_font = fonts.ui_font(9, bold=True)
        painter.setFont(pill_font)
        painter.setPen(QColor(Colors.ACCENT))
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, APP_VERSION)

        # 5. Concise Tagline (Fits comfortably without horizontal overflow)
        tagline_font = fonts.ui_font(10, bold=False)
        painter.setFont(tagline_font)
        painter.setPen(QColor(Colors.TEXT_SECONDARY))
        painter.drawText(QPointF(text_x, title_y + 24.0), "Fast, Modern & Secure Download Manager")

        # 6. Status Message & Percentage
        bar_x = 36.0
        bar_y = 184.0
        bar_w = w - 72.0
        bar_h = 6.0

        status_font = fonts.ui_font(9, bold=False)
        painter.setFont(status_font)
        painter.setPen(QColor(Colors.TEXT_SECONDARY))
        status_rect = QRectF(bar_x, bar_y - 24.0, bar_w - 60.0, 20.0)
        painter.drawText(
            status_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._message,
        )

        # Percentage text
        pct_font = fonts.ui_font(9, bold=True)
        painter.setFont(pct_font)
        painter.setPen(QColor(Colors.ACCENT))
        pct_rect = QRectF(bar_x + bar_w - 55.0, bar_y - 24.0, 55.0, 20.0)
        painter.drawText(
            pct_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{self._progress}%",
        )

        # 7. Progress Bar Track
        track_rect = QRectF(bar_x, bar_y, bar_w, bar_h)
        track_path = QPainterPath()
        track_path.addRoundedRect(track_rect, 3, 3)
        painter.fillPath(track_path, QColor(Colors.BG_LIGHT))
        painter.setPen(QColor(Colors.BORDER))
        painter.drawPath(track_path)

        # Progress Fill
        fill_w = (bar_w * self._progress) / 100.0
        if fill_w > 0:
            fill_rect = QRectF(bar_x, bar_y, max(fill_w, 6.0), bar_h)
            fill_path = QPainterPath()
            fill_path.addRoundedRect(fill_rect, 3, 3)

            prog_grad = QLinearGradient(bar_x, 0, bar_x + bar_w, 0)
            prog_grad.setColorAt(0.0, QColor(Colors.ACCENT))
            prog_grad.setColorAt(0.7, QColor(Colors.CYAN))
            prog_grad.setColorAt(1.0, QColor(Colors.GREEN))

            painter.fillPath(fill_path, prog_grad)

        # 8. Footer Copyright
        footer_font = fonts.ui_font(8, bold=False)
        painter.setFont(footer_font)
        painter.setPen(QColor(Colors.BORDER_LIGHT))
        footer_rect = QRectF(bar_x, h - 34.0, bar_w, 20.0)
        painter.drawText(
            footer_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "My-IDM — Free & Open Source",
        )

        painter.end()


EXIT_SPLASH_WIDTH = 480
EXIT_SPLASH_HEIGHT = 240


class IDMExitSplashScreen(QSplashScreen):
    """Custom frameless exit splash screen shown while shutting down engines and saving state."""

    def __init__(self, parent: QWidget | None = None):
        canvas = QPixmap(EXIT_SPLASH_WIDTH, EXIT_SPLASH_HEIGHT)
        canvas.fill(Qt.GlobalColor.transparent)
        super().__init__(canvas)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        self._message: str = "Closing My-IDM..."
        self._progress: int = 0
        self._logo_pixmap: QPixmap = get_app_logo_pixmap(54)
        if self._logo_pixmap.isNull() and LOGO_PNG.exists():
            pm = QPixmap(str(LOGO_PNG))
            if not pm.isNull():
                self._logo_pixmap = pm.scaled(
                    54, 54,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )

    @property
    def message(self) -> str:
        return self._message

    @property
    def progress(self) -> int:
        return self._progress

    def set_message(self, message: str, progress: int | None = None):
        """Update current shutdown step description and optional progress (0-100)."""
        self._message = message
        if progress is not None:
            self._progress = max(0, min(100, int(progress)))
        self.repaint()
        app = QApplication.instance()
        if app:
            app.processEvents()

    def set_progress(self, progress: int):
        """Update current progress percentage without changing message."""
        self._progress = max(0, min(100, int(progress)))
        self.repaint()
        app = QApplication.instance()
        if app:
            app.processEvents()

    def paintEvent(self, event):
        """Render high-DPI exit splash card."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w = float(self.width())
        h = float(self.height())
        radius = 14.0

        # 1. Background card with subtle border
        bg_rect = QRectF(1.0, 1.0, w - 2.0, h - 2.0)
        bg_gradient = QLinearGradient(0, 0, 0, h)
        bg_gradient.setColorAt(0.0, QColor("#161b22"))
        bg_gradient.setColorAt(0.6, QColor("#0f1319"))
        bg_gradient.setColorAt(1.0, QColor("#0d1117"))

        painter.setBrush(bg_gradient)
        painter.setPen(QColor(Colors.BORDER))
        painter.drawRoundedRect(bg_rect, radius, radius)

        # 2. Header accent line (purple / cyan gradient)
        top_accent_rect = QRectF(40.0, 1.0, w - 80.0, 2.0)
        accent_grad = QLinearGradient(40.0, 0, w - 40.0, 0)
        accent_grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        accent_grad.setColorAt(0.5, QColor(Colors.PURPLE))
        accent_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(top_accent_rect, accent_grad)

        # 3. Logo
        logo_x = 36.0
        logo_y = 36.0
        logo_size = 54.0
        if not self._logo_pixmap.isNull():
            glow_rect = QRectF(logo_x - 3, logo_y - 3, logo_size + 6, logo_size + 6)
            glow_path = QPainterPath()
            glow_path.addRoundedRect(glow_rect, 12, 12)
            painter.fillPath(glow_path, QColor(188, 140, 255, 20))

            clip_path = QPainterPath()
            clip_path.addRoundedRect(QRectF(logo_x, logo_y, logo_size, logo_size), 10, 10)
            painter.save()
            painter.setClipPath(clip_path)
            painter.drawPixmap(
                int(logo_x), int(logo_y), int(logo_size), int(logo_size),
                self._logo_pixmap
            )
            painter.restore()

        # 4. Title & Subtitle
        text_x = 108.0
        title_y = 58.0

        painter.setPen(QColor(Colors.TEXT))
        title_font = fonts.ui_font(18, bold=True)
        painter.setFont(title_font)
        painter.drawText(QPointF(text_x, title_y), "Shutting down My-IDM")

        subtitle_font = fonts.ui_font(10, bold=False)
        painter.setFont(subtitle_font)
        painter.setPen(QColor(Colors.TEXT_SECONDARY))
        painter.drawText(QPointF(text_x, title_y + 22.0), "Saving download sessions and releasing resources...")

        # 5. Status text & Percentage
        bar_x = 36.0
        bar_y = 156.0
        bar_w = w - 72.0
        bar_h = 6.0

        status_font = fonts.ui_font(9, bold=False)
        painter.setFont(status_font)
        painter.setPen(QColor(Colors.TEXT_SECONDARY))
        status_rect = QRectF(bar_x, bar_y - 22.0, bar_w - 60.0, 18.0)
        painter.drawText(
            status_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._message,
        )

        pct_font = fonts.ui_font(9, bold=True)
        painter.setFont(pct_font)
        painter.setPen(QColor(Colors.PURPLE))
        pct_rect = QRectF(bar_x + bar_w - 55.0, bar_y - 22.0, 55.0, 18.0)
        painter.drawText(
            pct_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{self._progress}%",
        )

        # 6. Progress bar
        track_rect = QRectF(bar_x, bar_y, bar_w, bar_h)
        track_path = QPainterPath()
        track_path.addRoundedRect(track_rect, 3, 3)
        painter.fillPath(track_path, QColor(Colors.BG_LIGHT))
        painter.setPen(QColor(Colors.BORDER))
        painter.drawPath(track_path)

        fill_w = (bar_w * self._progress) / 100.0
        if fill_w > 0:
            fill_rect = QRectF(bar_x, bar_y, max(fill_w, 6.0), bar_h)
            fill_path = QPainterPath()
            fill_path.addRoundedRect(fill_rect, 3, 3)

            prog_grad = QLinearGradient(bar_x, 0, bar_x + bar_w, 0)
            prog_grad.setColorAt(0.0, QColor(Colors.ACCENT))
            prog_grad.setColorAt(0.7, QColor(Colors.PURPLE))
            prog_grad.setColorAt(1.0, QColor(Colors.CYAN))

            painter.fillPath(fill_path, prog_grad)

        # 7. Footer
        footer_font = fonts.ui_font(8, bold=False)
        painter.setFont(footer_font)
        painter.setPen(QColor(Colors.BORDER_LIGHT))
        footer_rect = QRectF(bar_x, h - 30.0, bar_w, 18.0)
        painter.drawText(
            footer_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "Please wait while components exit cleanly...",
        )

        painter.end()

