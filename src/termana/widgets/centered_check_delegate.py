"""Centered item-view switches with matching mouse hit testing and native keyboard behavior."""

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)


class CenteredCheckDelegate(QStyledItemDelegate):
    def check_rect(self, option, index):
        prepared = QStyleOptionViewItem(option)
        self.initStyleOption(prepared, index)
        style = prepared.widget.style() if prepared.widget else QApplication.style()
        native = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, prepared, prepared.widget
        )
        return QStyle.alignedRect(
            prepared.direction, Qt.AlignmentFlag.AlignCenter, native.size(), option.rect
        )

    def paint(self, painter, option, index):
        prepared = QStyleOptionViewItem(option)
        self.initStyleOption(prepared, index)
        if not prepared.features & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator:
            return super().paint(painter, option, index)
        style = prepared.widget.style() if prepared.widget else QApplication.style()
        rect = self.check_rect(prepared, index)
        background = QStyleOptionViewItem(prepared)
        background.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter, prepared.widget)
        # Cocoa item-view primitives can disappear after a light/dark stylesheet
        # switch. Paint this small, palette-aware indicator consistently, while
        # Qt retains selection, focus, accessibility and the model's check state.
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = bool(prepared.state & QStyle.StateFlag.State_Enabled)
        group = QPalette.ColorGroup.Normal if enabled else QPalette.ColorGroup.Disabled
        selected = bool(prepared.state & QStyle.StateFlag.State_Selected)
        foreground = prepared.palette.color(
            group, QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        painter.setPen(QPen(foreground, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(rect).adjusted(.5, .5, -.5, -.5), 3, 3)
        if prepared.checkState != Qt.CheckState.Unchecked:
            pen = QPen(foreground, 1.7)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            def point(x, y):
                return QPointF(rect.x() + rect.width() * x, rect.y() + rect.height() * y)
            if prepared.checkState == Qt.CheckState.PartiallyChecked:
                painter.drawLine(point(.25, .5), point(.75, .5))
            else:
                painter.drawPolyline(QPolygonF([point(.24, .5), point(.43, .7), point(.77, .3)]))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        flags = model.flags(index)
        if not (flags & Qt.ItemFlag.ItemIsUserCheckable and flags & Qt.ItemFlag.ItemIsEnabled):
            return False
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            hit = event.button() == Qt.MouseButton.LeftButton and self.check_rect(
                option, index
            ).contains(event.position().toPoint())
            if hit and isinstance(self.parent(), QAbstractItemView):
                self.parent().setCurrentIndex(index)
                self.parent().setFocus(Qt.FocusReason.MouseFocusReason)
            return hit
        if event.type() == QEvent.Type.MouseButtonRelease:
            if event.button() != Qt.MouseButton.LeftButton or not self.check_rect(
                option, index
            ).contains(event.position().toPoint()):
                return False
        elif event.type() == QEvent.Type.KeyPress:
            if event.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Select):
                return False
        else:
            return False
        # PySide models may return either the original integer or an enum after
        # setData. Both representations must toggle, not become permanently On.
        checked = index.data(Qt.ItemDataRole.CheckStateRole) in (
            Qt.CheckState.Checked, Qt.CheckState.Checked.value,
        )
        return model.setData(
            index,
            Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked,
            Qt.ItemDataRole.CheckStateRole,
        )
