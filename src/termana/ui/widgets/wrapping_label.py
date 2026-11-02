"""Wrapped text whose minimum height follows its actual width and live font."""

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import QLabel, QWidget


class WrappingLabel(QLabel):
    """Prevent nested layouts from compressing a paragraph into overlapping rows."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        self._height_update_pending = False
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setMinimumWidth(0)

    def setText(self, text: str) -> None:
        super().setText(text)
        self._schedule_height_update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._schedule_height_update()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._schedule_height_update()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._schedule_height_update()

    def _schedule_height_update(self) -> None:
        # Defer until inherited fonts and the parent layout have settled. The
        # QObject context cancels this callback if a dialog is destroyed first.
        if not self._height_update_pending:
            self._height_update_pending = True
            QTimer.singleShot(0, self, self._update_wrapped_height)

    def _update_wrapped_height(self) -> None:
        self._height_update_pending = False
        if not self.isVisible() or not self.wordWrap():
            return
        # QLabel.heightForWidth includes the current minimum height. Remove our
        # previous bound before measuring, or a once-tall label can never shrink.
        if self.minimumHeight():
            self.setMinimumHeight(0)
        height = max(0, self.heightForWidth(self.width())) if self.text() else 0
        if height != self.minimumHeight():
            self.setMinimumHeight(height)
