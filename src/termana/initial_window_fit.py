"""One exposure-aware startup fit, without changing saved or later geometry."""

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from termana.dialog_geometry import fit_window_to_screen


class InitialWindowFit(QObject):
    def __init__(self, window: QWidget, *, allowed: Callable[[], bool]):
        super().__init__(window)
        self._window = window
        self._allowed = allowed
        self._handle = None
        self._done = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply)
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if self._done:
            return False
        if watched is self._window and event.type() == QEvent.Type.Show:
            handle = self._window.windowHandle()
            if handle is not None and handle is not self._handle:
                if self._handle is not None and isValid(self._handle):
                    self._handle.removeEventFilter(self)
                self._handle = handle
                handle.installEventFilter(self)
            self._schedule()
        elif watched is self._handle and event.type() == QEvent.Type.Expose:
            self._schedule()
        return False

    def _schedule(self):
        if not self._timer.isActive():
            self._timer.start(0)

    def _apply(self):
        handle = self._window.windowHandle()
        if not self._window.isVisible() or handle is None or not handle.isExposed():
            return
        self._done = True
        self._window.removeEventFilter(self)
        if self._handle is not None and isValid(self._handle):
            self._handle.removeEventFilter(self)
        if self._allowed():
            fit_window_to_screen(self._window)
