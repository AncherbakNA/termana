"""Native checkbox behavior with a palette-aware check/mixed glyph."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QCheckBox, QStyle, QStyleOptionButton


class ThemedCheckBox(QCheckBox):
    """Let Qt own label, focus, hit testing, signals and state transitions."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.checkState() == Qt.CheckState.Unchecked:
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        if rect.width() < 6 or rect.height() < 6:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            group = (
                option.palette.currentColorGroup()
                if self.isEnabled()
                else QPalette.ColorGroup.Disabled
            )
            pen = QPen(option.palette.color(group, QPalette.ColorRole.WindowText), 1.7)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)

            def point(x, y):
                return QPointF(rect.x() + rect.width() * x, rect.y() + rect.height() * y)

            if self.checkState() == Qt.CheckState.PartiallyChecked:
                painter.drawLine(point(0.25, 0.5), point(0.75, 0.5))
            else:
                painter.drawPolyline(
                    QPolygonF([point(0.24, 0.5), point(0.43, 0.7), point(0.77, 0.3)])
                )
        finally:
            painter.end()
