"""Single-line chrome text with the full value retained for help and accessibility."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QSizePolicy

from termana.ui_text import plain_text_tooltip


class ElidingLabel(QLabel):
    text_changed = Signal(str)

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text):
        super().setText(text)
        self.setToolTip(plain_text_tooltip(text))
        self.setAccessibleDescription(text)
        self.text_changed.emit(text)

    def paintEvent(self, event):
        painter = QPainter(self)
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width()
        )
        self.style().drawItemText(
            painter,
            self.contentsRect(),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.palette(),
            self.isEnabled(),
            text,
            self.foregroundRole(),
        )
