"""Theme-colored chevrons inside Qt's own form subcontrol rectangles."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QStyle, QStylePainter, QWidget


def paint_form_arrow(
    widget: QWidget,
    painter: QStylePainter,
    option,
    control: QStyle.ComplexControl,
    part: QStyle.SubControl,
    *,
    up: bool = False,
    enabled: bool = True,
) -> None:
    """Change only the glyph; style geometry and native input handling stay intact."""
    rect = widget.style().subControlRect(control, option, part, widget)
    if rect.width() < 6 or rect.height() < 6:
        return
    half_width = min(rect.width() / 4, max(3.0, widget.fontMetrics().height() / 5))
    half_height = min(rect.height() / 4, half_width / 2)
    center = QPointF(rect.center())
    direction = -1 if up else 1
    group = option.palette.currentColorGroup() if enabled else QPalette.ColorGroup.Disabled
    pen = QPen(option.palette.color(group, QPalette.ColorRole.ButtonText), 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolyline(
        QPolygonF(
            [
                center + QPointF(-half_width, -direction * half_height),
                center + QPointF(0, direction * half_height),
                center + QPointF(half_width, -direction * half_height),
            ]
        )
    )
