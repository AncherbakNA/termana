"""Centered terminal tab chrome and identity-bound tab drag initiation."""

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QIcon
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionTab, QStylePainter, QTabBar
from shiboken6 import isValid


class TerminalTabBar(QTabBar):
    cross_pane_drag_requested = Signal(object)
    drag_updated = Signal(object, object)
    drag_finished = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pressed_page = None
        self._dragging = False
        self._press_position = QPoint()

    def content_rect(self, index):
        # Same inset as the terminal-tab surface in the application stylesheet.
        return self.tabRect(index).adjusted(0, 4, 0, -2)

    def _center_controls(self):
        for index in range(self.count()):
            for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
                button = self.tabButton(index, side)
                if button is not None:
                    rect = self.content_rect(index)
                    on_left = side == QTabBar.ButtonPosition.LeftSide
                    if self.layoutDirection() == Qt.LayoutDirection.RightToLeft:
                        on_left = not on_left
                    # Native Windows metrics can leave the close widget on the
                    # tab's outer border. Position inside the painted surface,
                    # not just vertically relative to the native tab rectangle.
                    x = rect.left() + 6 if on_left else rect.right() - 5 - button.width()
                    button.move(
                        x, rect.top() + (rect.height() - button.height()) // 2
                    )

    def tabLayoutChange(self):
        super().tabLayoutChange()
        self._center_controls()

    def paintEvent(self, event):
        self._center_controls()
        painter = QStylePainter(self)
        for index in range(self.count()):
            if not self.isTabVisible(index):
                continue
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            label = self.tabText(index).replace("&&", "&")
            # QStyleOption exposes its QIcon by reference in PySide. Copy it
            # before clearing the option for frame-only native painting.
            icon = QIcon(option.icon)
            option.text = ""
            option.icon = QIcon()
            painter.drawControl(QStyle.ControlElement.CE_TabBarTab, option)
            rect = self.content_rect(index)
            # Reserve equal space on both sides, even on Cocoa where X is left.
            reserve = 16
            for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
                button = self.tabButton(index, side)
                if button is not None:
                    reserve = max(reserve, button.width() + 12)
            rect.adjust(reserve, 0, -reserve, 0)
            font = QFont(self.font())
            if index == self.currentIndex():
                font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            metrics = QFontMetrics(font)
            icon_width = min(self.iconSize().width(), rect.height()) if not icon.isNull() else 0
            gap = 6 if icon_width else 0
            label = metrics.elidedText(label, self.elideMode(), max(0, rect.width() - icon_width - gap))
            if icon_width:
                text_width = metrics.horizontalAdvance(label)
                left = rect.x() + (rect.width() - icon_width - gap - text_width) // 2
                icon.paint(painter, QRect(left, rect.y() + (rect.height() - icon_width) // 2,
                    icon_width, icon_width), Qt.AlignmentFlag.AlignCenter,
                    QIcon.Mode.Normal if self.isTabEnabled(index) else QIcon.Mode.Disabled)
                rect = QRect(left + icon_width + gap, rect.y(), text_width, rect.height())
            self.style().drawItemText(
                painter,
                rect,
                Qt.AlignmentFlag.AlignCenter,
                option.palette,
                self.isTabEnabled(index),
                label,
            )

    def mousePressEvent(self, event):
        self._pressed_page = None
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                self._pressed_page = self.parentWidget().widget(index)
                self._press_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed_page is not None and not isValid(self._pressed_page):
            self._pressed_page = None
            self._dragging = False
            self.drag_finished.emit(None, QPoint())
            return
        if self._pressed_page is not None and event.buttons() & Qt.MouseButton.LeftButton:
            position = event.position().toPoint()
            if (
                position - self._press_position
            ).manhattanLength() < QApplication.startDragDistance():
                return
            if self.property("crossPaneDragEnabled"):
                if not self._dragging:
                    self._dragging = True
                    self.setFocus(Qt.FocusReason.MouseFocusReason)
                    self.cross_pane_drag_requested.emit(self._pressed_page)
                self.drag_updated.emit(self._pressed_page, self.mapToGlobal(position))
            else:
                source = self.parentWidget().indexOf(self._pressed_page)
                target = self.tabAt(position)
                if self.isMovable() and source >= 0 and target >= 0 and source != target:
                    self.moveTab(source, target)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        page, dragging = self._pressed_page, self._dragging
        self._pressed_page = None
        self._dragging = False
        super().mouseReleaseEvent(event)
        if dragging:
            self.drag_finished.emit(page, self.mapToGlobal(event.position().toPoint()))

    def keyPressEvent(self, event):
        if self._dragging and event.key() == Qt.Key.Key_Escape:
            self._pressed_page = None
            self._dragging = False
            self.drag_finished.emit(None, QPoint())
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        if self._dragging:
            self._pressed_page = None
            self._dragging = False
            self.drag_finished.emit(None, QPoint())
        super().hideEvent(event)
