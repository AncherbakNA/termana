"""Keep clipped status messages readable without changing their lifetime."""

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QStatusBar, QWidget

from termana.ui_text import plain_text_tooltip


class ReadableStatusBar(QStatusBar):
    """Qt owns display/timers; details always follow its current message."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.messageChanged.connect(self._sync_message_details)

    @Slot(str)
    def _sync_message_details(self, message: str) -> None:
        self.setToolTip(plain_text_tooltip(message) if message else "")
        self.setAccessibleDescription(message)
