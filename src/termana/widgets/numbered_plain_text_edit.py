"""Plain text with a painted gutter; line numbers never enter the document."""

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from termana.i18n import tr


class _LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setAccessibleName(tr("Line numbers"))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self):
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)


class NumberedPlainTextEdit(QPlainTextEdit):
    """Number logical lines, including empty/trailing lines, not wrapped rows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_numbers = _LineNumbers(self)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self._update_gutter_width()

    def plain_text(self) -> str:
        # toPlainText() replaces NBSP with an ordinary space; keep command bytes.
        return self.document().toRawText().replace("\u2029", "\n")

    def line_number_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, *_args):
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)
        self._position_gutter()
        self.line_numbers.update()

    def _position_gutter(self):
        rect = self.contentsRect()
        self.line_numbers.setGeometry(
            QRect(rect.left(), rect.top(), self.line_number_width(), rect.height())
        )

    def _update_gutter(self, rect, dy):
        if dy:
            self.line_numbers.scroll(0, dy)
        else:
            self.line_numbers.update(0, rect.y(), self.line_number_width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_gutter()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, "line_numbers") and event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.PaletteChange,
        ):
            self._update_gutter_width()

    def paint_line_numbers(self, event):
        painter = QPainter(self.line_numbers)
        try:
            painter.fillRect(event.rect(), self.palette().color(QPalette.ColorRole.Base))
            painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            painter.setFont(self.font())
            block = self.firstVisibleBlock()
            top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
            while block.isValid() and top <= event.rect().bottom():
                height = self.blockBoundingRect(block).height()
                if block.isVisible() and top + height >= event.rect().top():
                    painter.drawText(
                        QRect(
                            0,
                            round(top),
                            self.line_numbers.width() - 8,
                            self.fontMetrics().height(),
                        ),
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                        str(block.blockNumber() + 1),
                    )
                top += height
                block = block.next()
        finally:
            painter.end()
