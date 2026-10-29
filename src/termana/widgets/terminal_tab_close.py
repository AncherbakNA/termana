"""Readable close affordance; the owning workspace still decides whether to close."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPalette, QPen
from PySide6.QtWidgets import QStyle, QTabBar, QToolButton
from shiboken6 import isValid

from termana.i18n import tr
from termana.qt_lifecycle import schedule_delete_later


class TerminalTabCloseButton(QToolButton):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("terminalTabCloseButton")
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(20, 20)
        self.setToolTip(tr("Close tab"))
        self.setAccessibleName(tr("Close tab"))

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            group = (
                self.palette().currentColorGroup()
                if self.isEnabled()
                else QPalette.ColorGroup.Disabled
            )
            pen = QPen(self.palette().color(group, QPalette.ColorRole.ButtonText), 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            center = QRectF(self.rect()).center()
            for direction in (-1, 1):
                painter.drawLine(
                    center + QPointF(-3.5, -3.5 * direction),
                    center + QPointF(3.5, 3.5 * direction),
                )
        finally:
            painter.end()


def install_tab_close_button(tabs, index):
    """Keep Qt's chosen side and resolve page identity again after a move/reorder."""
    if not tabs.tabsClosable():
        return
    bar = tabs.tabBar()
    position = QTabBar.ButtonPosition(
        bar.style().styleHint(QStyle.StyleHint.SH_TabBar_CloseButtonPosition, None, bar)
    )
    previous = bar.tabButton(index, position)
    if isinstance(previous, TerminalTabCloseButton):
        return
    page = tabs.widget(index)
    button = TerminalTabCloseButton(bar)

    def request_close():
        if not isValid(tabs) or not isValid(page) or not isValid(button) or not tabs.tabsClosable():
            return
        current = tabs.indexOf(page)
        if current >= 0 and bar.tabButton(current, position) is button:
            tabs.tabCloseRequested.emit(current)

    button.clicked.connect(request_close)
    bar.setTabButton(index, position, button)
    if previous is not None:
        schedule_delete_later(previous)
