"""Bounded, selectable inline diagnostics that cannot displace dialog actions."""

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QFrame, QScrollArea, QSizePolicy, QWidget

from termana.widgets.wrapping_label import WrappingLabel


class InlineErrorArea(QScrollArea):
    """Keep short errors compact and make long errors independently scrollable."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fit_pending = False
        self._height_limit_provider: Callable[[], int] | None = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.label = WrappingLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setWidget(self.label)
        self.viewport().installEventFilter(self)
        self.hide()

    def show_message(self, message: str) -> None:
        self.label.setText(message)
        self.label.show()
        self.show()
        self.verticalScrollBar().setValue(0)
        self._schedule_fit()

    def set_height_limit_provider(self, provider: Callable[[], int]) -> None:
        """Allow a pinned footer to reserve room for its essential actions."""
        self._height_limit_provider = provider
        self._schedule_fit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_fit()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._schedule_fit()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.viewport() and event.type() == QEvent.Type.Resize:
            self._schedule_fit()
        return super().eventFilter(watched, event)

    def _schedule_fit(self) -> None:
        if not self._fit_pending:
            self._fit_pending = True
            QTimer.singleShot(0, self, self._fit)

    def _fit(self) -> None:
        self._fit_pending = False
        # WrappingLabel's earlier minimum must not keep a shortened error tall.
        self.label.setMinimumHeight(0)
        line_height = self.label.fontMetrics().height()
        text_height = max(line_height, self.label.heightForWidth(max(1, self.viewport().width())))
        self.label.setMinimumHeight(text_height)
        height = min(text_height + 4, line_height * 5 + 4)
        if self._height_limit_provider is not None:
            height = min(height, max(line_height + 4, self._height_limit_provider()))
        if self.minimumHeight() != height or self.maximumHeight() != height:
            self.setFixedHeight(height)
