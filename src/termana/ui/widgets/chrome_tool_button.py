"""Compact tool chrome with anchored help and an explicit keyboard focus ring."""

from html import unescape

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QLabel, QStyle, QStyleOptionToolButton, QStylePainter, QToolButton


class _AnchoredTooltip(QObject):
    def __init__(self, button, placement):
        super().__init__(button)
        self.placement = placement
        self.popup = None
        button.installEventFilter(self)

    def eventFilter(self, button, event):
        if event.type() == QEvent.Type.ToolTip and button.toolTip():
            if self.popup is None:
                self.popup = QLabel(button, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
                self.popup.setObjectName("anchoredControlTooltip")
                self.popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
                self.popup.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                self.popup.setTextFormat(Qt.TextFormat.PlainText)
                self.popup.setWordWrap(True)
                self.popup.setMargin(7)
                self.timer = QTimer(self.popup)
                self.timer.setSingleShot(True)
                self.timer.timeout.connect(self.popup.hide)
            self.popup.setFont(button.font())
            text = button.toolTip()
            if text.startswith("<qt>") and text.endswith("</qt>"):
                text = unescape(text[4:-5])
            self.popup.setText(text)
            screen = button.screen().availableGeometry()
            self.popup.setMaximumWidth(min(360, max(1, screen.width() - 12)))
            self.popup.adjustSize()
            point = (
                QPoint(button.width() + 4, 0)
                if self.placement == "right"
                else QPoint(0, button.height() + 2)
            )
            point = button.mapToGlobal(point)
            point.setX(max(screen.left(), min(point.x(), screen.right() - self.popup.width() + 1)))
            point.setY(max(screen.top(), min(point.y(), screen.bottom() - self.popup.height() + 1)))
            self.popup.move(point)
            self.popup.show()
            self.timer.start(8000)
            event.accept()
            return True
        if self.popup is not None and event.type() in (
            QEvent.Type.Leave, QEvent.Type.Hide, QEvent.Type.MouseButtonPress,
            QEvent.Type.FocusOut, QEvent.Type.Move, QEvent.Type.Resize,
        ):
            self.popup.hide()
        return super().eventFilter(button, event)


def anchor_tooltip(button, *, placement="below"):
    """Use the control's current geometry, not a stale/global cursor location."""
    if not hasattr(button, "_anchored_tooltip"):
        button._anchored_tooltip = _AnchoredTooltip(button, placement)


class ChromeToolButton(QToolButton):
    """Keep native button semantics, replacing the platform's dotted focus primitive."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keyboard_focus = False
        self.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() not in (
            Qt.FocusReason.MouseFocusReason,
            Qt.FocusReason.ActiveWindowFocusReason,
        )
        super().focusInEvent(event)
        self.update()

    def paintEvent(self, event):
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.state &= ~QStyle.StateFlag.State_HasFocus
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)
        if self.hasFocus() and self._keyboard_focus:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(self.palette().highlight().color(), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 5, 5)
