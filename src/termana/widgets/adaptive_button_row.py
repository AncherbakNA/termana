"""A small sidebar button row which stacks before captions become clipped."""

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QGridLayout, QWidget

from termana.ui_typography import scale_widget_font


class AdaptiveButtonRow(QWidget):
    def __init__(self, buttons, parent=None):
        super().__init__(parent)
        self._buttons = tuple(buttons)
        self._pending = False
        self._columns = 0
        self._visible_buttons = ()
        self.row_layout = QGridLayout(self)
        self.row_layout.setContentsMargins(0, 0, 0, 0)
        for column, button in enumerate(self._buttons):
            self.row_layout.addWidget(button, 0, column)
            button.installEventFilter(self)
        scale_widget_font(self, 1.0)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        # A horizontal layout's summed minimum can prevent the parent from ever
        # becoming narrow enough to trigger stacking (notably in QScrollArea).
        # One complete control is the horizontal minimum; reflow owns the rest.
        margins = self.row_layout.contentsMargins()
        widest = max(
            (
                self.row_layout.itemAt(index).minimumSize().width()
                for index in range(self.row_layout.count())
            ),
            default=0,
        )
        hint.setWidth(widest + margins.left() + margins.right())
        return hint

    def eventFilter(self, watched, event):
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.Polish,
            QEvent.Type.ShowToParent,
            QEvent.Type.HideToParent,
        ):
            self._schedule()
        return super().eventFilter(watched, event)

    def event(self, event):
        if event.type() == QEvent.Type.LayoutRequest and hasattr(self, "row_layout"):
            self._schedule()
        return super().event(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule()

    def _schedule(self):
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self, self._reflow)

    def _reflow(self):
        self._pending = False
        spacing = max(0, self.row_layout.spacing())
        buttons = tuple(button for button in self._buttons if not button.isHidden())
        widths = [button.sizeHint().width() for button in buttons]
        columns = 1
        for candidate in range(len(widths), 1, -1):
            if len(widths) == 4 and candidate == 3:
                continue  # Prefer a balanced 2-by-2 group over three plus one.
            required = candidate * max(widths)
            if required + spacing * (candidate - 1) <= self.width():
                columns = candidate
                break
        if columns == self._columns and buttons == self._visible_buttons:
            return
        self._columns = columns
        self._visible_buttons = buttons
        while self.row_layout.count():
            self.row_layout.takeAt(0)
        for index, button in enumerate(buttons):
            self.row_layout.addWidget(button, index // columns, index % columns)
        for column in range(len(self._buttons)):
            self.row_layout.setColumnStretch(column, int(column < columns))
        self.updateGeometry()
