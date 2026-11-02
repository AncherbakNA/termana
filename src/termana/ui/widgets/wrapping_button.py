"""A normal text push button that can wrap inside narrow sidebar layouts."""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QPushButton, QSizePolicy, QStyle, QStyleOptionButton, QStylePainter

MINIMUM_TEXT_COLUMNS = 8
_TEXT_FLAGS = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextShowMnemonic


class WrappingPushButton(QPushButton):
    """Keep Qt's text, accessibility and input; only wrap the painted caption."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setProperty("actionRole", "secondary")
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _contents(self, width):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.rect = QRect(0, 0, width, super().sizeHint().height())
        rect = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        return option, rect

    def minimumSizeHint(self):
        base = super().minimumSizeHint()
        if not self.icon().isNull() or self.menu() is not None:
            return base
        option, rect = self._contents(base.width())
        inset = max(0, option.rect.width() - rect.width())
        width = MINIMUM_TEXT_COLUMNS * self.fontMetrics().horizontalAdvance("M") + inset
        return QSize(min(base.width(), width), base.height())

    def sizeHint(self):
        base = super().sizeHint()
        if not self.icon().isNull() or self.menu() is not None:
            return base
        option, rect = self._contents(base.width())
        text_width = self.fontMetrics().horizontalAdvance(self.text().replace("&", ""))
        return QSize(
            max(base.width(), text_width + option.rect.width() - rect.width()), base.height()
        )

    def _text_flags(self, width):
        words = self.text().replace("&", "").split()
        if any(self.fontMetrics().horizontalAdvance(word) > width for word in words):
            return _TEXT_FLAGS | Qt.TextFlag.TextWrapAnywhere
        return _TEXT_FLAGS

    def heightForWidth(self, width):
        if not self.icon().isNull() or self.menu() is not None:
            return super().sizeHint().height()
        option, rect = self._contents(width)
        text_height = (
            self.fontMetrics()
            .boundingRect(
                QRect(0, 0, max(1, rect.width()), 1_000_000),
                self._text_flags(rect.width()),
                self.text(),
            )
            .height()
        )
        return max(option.rect.height(), text_height + option.rect.height() - rect.height())

    def paintEvent(self, event):
        if not self.icon().isNull() or self.menu() is not None:
            return super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        text = option.text
        option.text = ""
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        if option.state & QStyle.StateFlag.State_Sunken:
            rect.translate(
                self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonShiftHorizontal, option, self),
                self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonShiftVertical, option, self),
            )
        painter.drawItemText(
            rect,
            int(
                self._text_flags(rect.width())
                | (
                    Qt.TextFlag.TextHideMnemonic
                    if not self.style().styleHint(
                        QStyle.StyleHint.SH_UnderlineShortcut, option, self
                    )
                    else Qt.TextFlag(0)
                )
            ),
            option.palette,
            self.isEnabled(),
            text,
            QPalette.ColorRole.ButtonText,
        )
