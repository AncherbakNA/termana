"""An accessible full-screen command without session or focus side effects."""

import sys

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt
from PySide6.QtGui import QKeySequence

from termana.i18n import tr
from termana.ui_actions import create_window_action


def fullscreen_shortcuts(platform_name=None):
    # Command belongs to the Mac app, not the remote terminal. Do not reserve
    # F11 (or any other terminal function key) on the other platforms.
    if (platform_name or sys.platform) == "darwin":
        return (QKeySequence("Ctrl+Meta+F"),)
    return ()


class WindowFullscreenController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._restore_maximized = window.isMaximized()
        self.action = create_window_action(
            window,
            tr("Enter full screen"),
            "toggleFullScreenAction",
            self.toggle,
            status_tip=tr("Change window display mode without closing terminals"),
            shortcuts=fullscreen_shortcuts(),
            auto_repeat=False,
            checkable=True,
        )
        window.addAction(self.action)
        window.installEventFilter(self)
        self._sync()

    def toggle(self, _checked=False):
        if self._window.isFullScreen():
            if self._restore_maximized:
                self._window.showMaximized()
            else:
                self._window.showNormal()
        else:
            self._window.showFullScreen()
        self._sync()

    def _sync(self):
        fullscreen = self._window.isFullScreen()
        with QSignalBlocker(self.action):
            self.action.setChecked(fullscreen)
        self.action.setText(tr("Exit full screen") if fullscreen else tr("Enter full screen"))

    def eventFilter(self, watched, event):
        if watched is self._window and event.type() == QEvent.Type.WindowStateChange:
            # Native traffic-light entry also reaches this path. Remember the
            # previous state instead of always returning a maximized window to
            # a smaller normal geometry.
            old = event.oldState()
            if self._window.isFullScreen() and not old & Qt.WindowState.WindowFullScreen:
                self._restore_maximized = bool(old & Qt.WindowState.WindowMaximized)
            self._sync()
        return False
