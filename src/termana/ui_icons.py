"""Small palette-aware vector icons used by Termana's application chrome."""

from typing import Literal

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from termana.theme import DEFAULT_THEME

InterfaceIconName = Literal[
    "new_connection",
    "terminal",
    "desktop",
    "settings",
    "upload",
    "download",
    "stop",
    "folder",
    "folder_open",
    "file",
    "arrow_left",
    "arrow_right",
    "arrow_up",
    "refresh",
    "copy",
    "server",
    "split",
    "split_rows",
    "pane_grid",
    "single_pane",
    "tabs",
    "details",
    "search",
    "help",
]


def application_icon() -> QIcon:
    """Build the reusable Termana brand mark at common desktop icon sizes."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128):
        scale = 2
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(DEFAULT_THEME.accent))
            inset = max(1.0, size * 0.055)
            radius = size * 0.24
            painter.drawRoundedRect(
                QRectF(inset, inset, size - inset * 2, size - inset * 2),
                radius,
                radius,
            )

            glyph_pen = QPen(QColor(DEFAULT_THEME.accent_text), max(1.5, size * 0.075))
            glyph_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            glyph_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(glyph_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(
                QPointF(size * 0.29, size * 0.36),
                QPointF(size * 0.45, size * 0.50),
            )
            painter.drawLine(
                QPointF(size * 0.45, size * 0.50),
                QPointF(size * 0.29, size * 0.64),
            )
            painter.drawLine(
                QPointF(size * 0.52, size * 0.65),
                QPointF(size * 0.72, size * 0.65),
            )
        finally:
            painter.end()
        icon.addPixmap(pixmap)
    return icon


def interface_icon(name: InterfaceIconName, color: str, *, size: int = 18) -> QIcon:
    """Render one crisp monochrome icon without a platform-specific asset bundle."""
    return QIcon(_interface_pixmap(name, color, size=size))


def selection_interface_icon(
    name: InterfaceIconName, color: str, selected_color: str, *, size: int = 18
) -> QIcon:
    """Keep normal and selected glyphs legible without Qt's native recoloring."""
    icon = interface_icon(name, color, size=size)
    icon.addPixmap(_interface_pixmap(name, selected_color, size=size), QIcon.Mode.Selected)
    return icon


def _interface_pixmap(name: InterfaceIconName, color: str, *, size: int) -> QPixmap:
    """Render one crisp monochrome icon without a platform-specific asset bundle."""
    if size < 12:
        raise ValueError("Interface icon size must be at least 12 pixels")
    icon_color = QColor(color)
    if not icon_color.isValid():
        raise ValueError("Interface icon color must be a valid Qt color")

    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 18, size / 18)
        pen = QPen(icon_color, 1.65)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if name == "new_connection":
            painter.drawRoundedRect(QRectF(2, 2, 14, 10), 1.5, 1.5)
            painter.drawLine(QPointF(9, 12), QPointF(9, 16))
            painter.drawLine(QPointF(6, 16), QPointF(12, 16))
            painter.drawLine(QPointF(9, 9), QPointF(9, 4.5))
            painter.drawLine(QPointF(6.5, 7), QPointF(9, 4.5))
            painter.drawLine(QPointF(11.5, 7), QPointF(9, 4.5))
        elif name == "desktop":
            painter.drawRoundedRect(QRectF(1.5, 2, 15, 10.5), 1.3, 1.3)
            painter.drawLine(QPointF(9, 12.5), QPointF(9, 16))
            painter.drawLine(QPointF(5.5, 16), QPointF(12.5, 16))
        elif name == "terminal":
            painter.drawRoundedRect(QRectF(1.75, 3.0, 14.5, 12.0), 2.4, 2.4)
            painter.drawLine(QPointF(5.0, 7.0), QPointF(7.1, 9.0))
            painter.drawLine(QPointF(7.1, 9.0), QPointF(5.0, 11.0))
            painter.drawLine(QPointF(9.0, 11.0), QPointF(12.5, 11.0))
        elif name in ("upload", "download"):
            painter.drawLine(QPointF(3, 12), QPointF(3, 16))
            painter.drawLine(QPointF(3, 16), QPointF(15, 16))
            painter.drawLine(QPointF(15, 16), QPointF(15, 12))
            top, tip = (11, 2) if name == "upload" else (2, 11)
            painter.drawLine(QPointF(9, top), QPointF(9, tip))
            wing = tip + 3 if name == "upload" else tip - 3
            painter.drawLine(QPointF(6, wing), QPointF(9, tip))
            painter.drawLine(QPointF(12, wing), QPointF(9, tip))
        elif name == "stop":
            painter.drawRoundedRect(QRectF(2, 2, 14, 14), 2, 2)
            painter.drawLine(QPointF(6, 6), QPointF(12, 12))
            painter.drawLine(QPointF(12, 6), QPointF(6, 12))
        elif name == "settings":
            painter.drawLine(QPointF(2.5, 5.0), QPointF(15.5, 5.0))
            painter.drawLine(QPointF(2.5, 9.0), QPointF(15.5, 9.0))
            painter.drawLine(QPointF(2.5, 13.0), QPointF(15.5, 13.0))
            painter.setBrush(icon_color)
            painter.drawEllipse(QPointF(6.0, 5.0), 1.55, 1.55)
            painter.drawEllipse(QPointF(12.0, 9.0), 1.55, 1.55)
            painter.drawEllipse(QPointF(8.0, 13.0), 1.55, 1.55)
        elif name in ("folder", "folder_open"):
            path = QPainterPath(QPointF(2, 15))
            path.lineTo(2, 4)
            path.lineTo(7, 4)
            path.lineTo(9, 6)
            path.lineTo(15.5, 6)
            path.lineTo(15.5, 15)
            path.closeSubpath()
            painter.drawPath(path)
            if name == "folder_open":
                opened = QPainterPath(QPointF(2, 15))
                opened.lineTo(4, 8)
                opened.lineTo(17, 8)
                opened.lineTo(15, 15)
                opened.closeSubpath()
                painter.drawPath(opened)
        elif name == "file":
            page = QPainterPath(QPointF(4, 1.5))
            page.lineTo(10, 1.5)
            page.lineTo(14, 5.5)
            page.lineTo(14, 16.5)
            page.lineTo(4, 16.5)
            page.closeSubpath()
            painter.drawPath(page)
            painter.drawLine(QPointF(10, 1.5), QPointF(10, 5.5))
            painter.drawLine(QPointF(10, 5.5), QPointF(14, 5.5))
            painter.drawLine(QPointF(6.5, 9), QPointF(11.5, 9))
            painter.drawLine(QPointF(6.5, 12), QPointF(11.5, 12))
        elif name in ("arrow_left", "arrow_right"):
            tip, tail = (3, 15) if name == "arrow_left" else (15, 3)
            shoulder = 8 if name == "arrow_left" else 10
            painter.drawLine(QPointF(tip, 9), QPointF(tail, 9))
            painter.drawLine(QPointF(tip, 9), QPointF(shoulder, 4))
            painter.drawLine(QPointF(tip, 9), QPointF(shoulder, 14))
        elif name == "arrow_up":
            painter.drawLine(QPointF(9, 3), QPointF(9, 15))
            painter.drawLine(QPointF(9, 3), QPointF(4, 8))
            painter.drawLine(QPointF(9, 3), QPointF(14, 8))
        elif name == "refresh":
            painter.drawArc(QRectF(3, 3, 12, 12), 45 * 16, 270 * 16)
            painter.drawLine(QPointF(14, 4), QPointF(10, 4))
            painter.drawLine(QPointF(14, 4), QPointF(14, 8))
        elif name == "copy":
            painter.drawRoundedRect(QRectF(2, 2, 10, 11), 1, 1)
            painter.drawRoundedRect(QRectF(6, 6, 10, 10), 1, 1)
        elif name == "server":
            for top in (2.5, 10):
                painter.drawRoundedRect(QRectF(2, top, 14, 5.5), 1.3, 1.3)
                painter.drawPoint(QPointF(5, top + 2.75))
                painter.drawLine(QPointF(9, top + 2.75), QPointF(13, top + 2.75))
        elif name in ("split", "split_rows", "pane_grid", "single_pane"):
            painter.drawRoundedRect(QRectF(2, 2.5, 14, 13), 1.4, 1.4)
            if name in ("split", "pane_grid"):
                painter.drawLine(QPointF(9, 2.5), QPointF(9, 15.5))
            if name in ("split_rows", "pane_grid"):
                painter.drawLine(QPointF(2, 9), QPointF(16, 9))
        elif name == "tabs":
            painter.drawRoundedRect(QRectF(2, 6, 14, 10), 1.3, 1.3)
            painter.drawLine(QPointF(4, 3), QPointF(14, 3))
            painter.drawLine(QPointF(7, 6), QPointF(7, 9))
            painter.drawLine(QPointF(11, 6), QPointF(11, 9))
        elif name == "search":
            painter.drawEllipse(QRectF(2, 2, 10, 10))
            painter.drawLine(QPointF(11, 11), QPointF(16, 16))
        elif name == "details":
            shield = QPainterPath(QPointF(9, 1.5))
            shield.lineTo(15, 4)
            shield.lineTo(15, 9)
            shield.quadTo(14, 13.5, 9, 16.5)
            shield.quadTo(4, 13.5, 3, 9)
            shield.lineTo(3, 4)
            shield.closeSubpath()
            painter.drawPath(shield)
            painter.drawLine(QPointF(6, 9), QPointF(8, 11))
            painter.drawLine(QPointF(8, 11), QPointF(12, 7))
        elif name == "help":
            painter.drawEllipse(QRectF(1.5, 1.5, 15, 15))
            question = QPainterPath(QPointF(6.5, 6.5))
            question.cubicTo(6.5, 3.5, 12, 3.5, 11.5, 7)
            question.cubicTo(11.5, 8.5, 9, 8.5, 9, 10.5)
            painter.drawPath(question)
            painter.drawPoint(QPointF(9, 13.5))
        else:
            raise ValueError(f"Unsupported interface icon: {name}")
    finally:
        painter.end()
    return pixmap
