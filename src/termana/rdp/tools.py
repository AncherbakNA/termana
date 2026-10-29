"""Explicit RDP session tools: local view policy, safe actions and no secret reuse."""

import time
from collections import deque

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from termana.dialog_geometry import fit_dialog_to_screen
from termana.i18n import tr
from termana.rdp.screenshot import save_screenshot

SHORTCUTS = {
    "Windows security (Ctrl+Alt+Delete)": (0x11, 0x12, 0x2E),
    "Task Manager (Ctrl+Shift+Esc)": (0x11, 0x10, 0x1B),
    "Run (Win+R)": (0x5B, 0x52),
    "File Explorer (Win+E)": (0x5B, 0x45),
    "Lock Windows (Win+L)": (0x5B, 0x4C),
    "Switch app (Alt+Tab)": (0x12, 0x09),
}


class DesktopFullscreen(QDialog):
    def __init__(self, tools):
        super().__init__(tools.page.window())
        self.tools = tools
        self.setObjectName("rdpFullscreenWindow")
        self.setWindowTitle(tools.page.display_title)
        self.setModal(False)
        self.finished.connect(tools.exit_fullscreen)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.setContentsMargins(10, 6, 10, 6)
        release = QKeySequence("Ctrl+Shift+F12").toString(QKeySequence.SequenceFormat.NativeText)
        caption = QLabel(tr("Remote desktop · release keyboard: {p0}", p0=release), self)
        caption.setWordWrap(True)
        header.addWidget(caption, 1)
        back = QPushButton(tr("Return to tab"), self)
        back.setObjectName("rdpReturnToTabButton")
        back.setProperty("actionRole", "secondary")
        chord = QKeySequence("Ctrl+Shift+F11").toString(QKeySequence.SequenceFormat.NativeText)
        back.setToolTip(tr("Return to tab: {p0}", p0=chord))
        back.clicked.connect(tools.exit_fullscreen)
        header.addWidget(back)
        root.addLayout(header)
        root.addWidget(tools.page.desktop_view, 1)
        # QStackedWidget.removeWidget explicitly hides the view. Reparenting it
        # does not clear that flag, even after the fullscreen window is exposed.
        tools.page.desktop_view.show()

    def closeEvent(self, event):
        self.tools.exit_fullscreen()
        event.accept()


class RdpSessionTools(QObject):
    def __init__(self, page, *, clock=time.monotonic):
        super().__init__(page)
        self.page = page
        self.clock = clock
        self._connected_at = None
        self._elapsed = 0
        self._frames = 0
        self._arrivals = deque(maxlen=120)
        self.fullscreen = None
        self._chooser = None
        self._details = None
        self._choosing = False
        self._feedback_text = ""
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(4000)
        self._feedback_timer.timeout.connect(self._clear_feedback)
        self.toolbar = QToolBar(page)
        self.toolbar.setObjectName("rdpSessionTools")
        self.toolbar.setMovable(False)
        self.toolbar.setFloatable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._menu_buttons = []
        self._statistics_layout_pending = False
        self.view_menu = self._menu("View")
        group = QActionGroup(self)
        group.setExclusive(True)
        self.zoom_actions = {}
        for caption, value in (("Fit to window", None), ("Actual size (1:1)", 100),
                               ("50%", 50), ("75%", 75), ("125%", 125),
                               ("150%", 150), ("200%", 200)):
            action = self._action(self.view_menu, caption, lambda _checked=False, zoom=value:
                                  page.desktop_view.set_zoom(zoom), checkable=True)
            group.addAction(action)
            self.zoom_actions[value] = action
        self.zoom_actions[None].setChecked(True)
        self.view_menu.addSeparator()
        self.fullscreen_action = self._action(self.view_menu, "Full screen", self.enter_fullscreen)
        self.privacy_action = self._action(self.view_menu, "Hide screen locally", self.set_private,
                                           checkable=True)
        self.privacy_action.setToolTip(tr("Hide the image and block input without disconnecting."))
        input_menu = self._menu("Input")
        self.view_only_action = self._action(input_menu, "View only", self.set_view_only,
                                             checkable=True)
        self.view_only_action.setToolTip(tr("Watch without sending keyboard or mouse input."))
        policy_menu = input_menu.addMenu(tr("Keyboard shortcuts"))
        policies = QActionGroup(self)
        policies.setExclusive(True)
        self.keyboard_actions = {}
        for title, value in (("Send to remote", "remote"), ("Keep on this computer", "local"),
                             ("Remote only in full screen", "fullscreen")):
            action = self._action(policy_menu, title,
                                  lambda _checked=False, mode=value: self.set_keyboard_mode(mode),
                                  checkable=True)
            policies.addAction(action)
            self.keyboard_actions[value] = action
        self.keyboard_actions["remote"].setChecked(True)
        self.capture_action = self._action(input_menu, "Capture keyboard", page.surface.capture_keyboard)
        release_keys = QKeySequence("Ctrl+Shift+F12").toString(QKeySequence.SequenceFormat.NativeText)
        self.release_action = self._action(input_menu, tr("Release keyboard ({p0})", p0=release_keys),
                                           page.surface.release_capture)
        self.release_action.setToolTip(tr("Always local. Click the desktop to capture again."))
        input_menu.addSeparator()
        self.shortcut_actions = {}
        for caption, keys in SHORTCUTS.items():
            self.shortcut_actions[caption] = self._action(
                input_menu, caption, lambda _checked=False, keys=keys: self.send_shortcut(keys)
            )
        session_menu = self._menu("Session")
        self.snapshot_action = self._action(session_menu, "Save screenshot…", self.request_screenshot)
        self.snapshot_action.setToolTip(tr("Save only the remote desktop as a new PNG file."))
        self.details_action = self._action(session_menu, "Connection details…", self.show_details)
        self.duplicate_action = self._action(session_menu, "New session to this server",
                                              self.duplicate)
        self.duplicate_action.setToolTip(tr(
            "New sign-in, no password or trust copied. Windows may limit sessions per user."
        ))
        self.other_user_action = self._action(session_menu, "Reconnect with another user…",
                                               page.reconnect_as_other_user)
        self.stats = QLabel(tr("Not connected"), page)
        self.stats.setObjectName("rdpSessionStatistics")
        self.stats.setToolTip(tr("Connected time · desktop frames received in the last second"))
        self.stats.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.stats.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._stats_action = self.toolbar.addWidget(self.stats)
        self.toolbar.installEventFilter(self)
        self._schedule_statistics_layout()
        self.fullscreen_placeholder = QWidget(page.stack)
        layout = QVBoxLayout(self.fullscreen_placeholder)
        layout.addStretch()
        label = QLabel(tr("This desktop is open in full screen."), self.fullscreen_placeholder)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        back = QPushButton(tr("Return to tab"), self.fullscreen_placeholder)
        back.clicked.connect(self.exit_fullscreen)
        layout.addWidget(back, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()
        page.stack.addWidget(self.fullscreen_placeholder)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        page.surface.frame_changed.connect(self._frame)
        page.surface.exit_fullscreen_requested.connect(self.exit_fullscreen)
        page.surface.capture_released.connect(self._capture_released)
        page.surface.eligibility_changed.connect(self._mode_status)
        page.controller.state_changed.connect(self._state)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self.sync()

    def _menu(self, text):
        menu = QMenu(tr(text), self.page)
        action = self.toolbar.addAction(tr(text))
        action.setMenu(menu)
        button = self.toolbar.widgetForAction(action)
        button.setProperty("rdpToolMenu", True)
        button.setText(tr(text) + " \u25be")
        button.setAccessibleName(tr(text))
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu_buttons.append(button)
        return menu

    def eventFilter(self, watched, event):
        if self._local_exit_from_owned_ui(watched, event):
            return True
        if watched is self.toolbar and event.type() in {
            QEvent.Type.Resize,QEvent.Type.FontChange,QEvent.Type.StyleChange,QEvent.Type.Show,
        }:
            self._schedule_statistics_layout()
        return False

    def _local_exit_from_owned_ui(self, watched, event):
        """Keep the exit local even when a session menu/toolbar owns the key.

        This observes Qt events in this page only, not system-wide input. Never
        intercept another tab/window, an authentication form or a modal dialog.
        """
        if (not isinstance(event, QKeyEvent) or not isinstance(watched, QWidget)
                or event.type() not in {QEvent.Type.ShortcutOverride,
                                        QEvent.Type.KeyPress, QEvent.Type.KeyRelease}
                or self.page._shutdown or self.page.controller.state != "Connected"
                or not self._eligible() or not self.page.surface.isActiveWindow()
                or QApplication.activeModalWidget() is not None
                or event.modifiers() != (Qt.KeyboardModifier.ControlModifier
                                         | Qt.KeyboardModifier.ShiftModifier)):
            return False
        fullscreen_exit = event.key() == Qt.Key.Key_F11 and self.fullscreen is not None
        if event.key() != Qt.Key.Key_F12 and not fullscreen_exit:
            return False
        parent = watched
        while parent is not None and parent is not self.page and parent is not self.fullscreen:
            parent = parent.parentWidget()
        if parent is None:
            return False
        event.accept()
        if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
            # Close only this page's popup chain; unrelated windows stay intact.
            for _ in range(8):
                popup = QApplication.activePopupWidget()
                if not isinstance(popup, QMenu):
                    break
                ancestor = popup
                while ancestor is not None and ancestor is not self.page:
                    ancestor = ancestor.parentWidget()
                if ancestor is None:
                    break
                popup.close()
            if fullscreen_exit:
                self.page.surface.release_input()
                self.exit_fullscreen()
            else:
                self.page.surface.release_capture()
        return True

    def _schedule_statistics_layout(self):
        if not self._statistics_layout_pending:
            self._statistics_layout_pending = True
            QTimer.singleShot(0,self,self._layout_statistics)

    def _layout_statistics(self):
        self._statistics_layout_pending = False
        if self.page._shutdown:
            return
        for button in self._menu_buttons:
            button.ensurePolished()
            needed = max(button.sizeHint().width(),button.fontMetrics().horizontalAdvance(button.text())+24)
            if button.minimumWidth()!=needed:
                button.setMinimumWidth(needed)
        # Prefer complete action names over a clipped clock. QToolBar retains
        # its native overflow for genuinely narrower panes; do not elide every
        # menu to make room for non-essential statistics.
        required = sum(button.minimumWidth() for button in self._menu_buttons)+self.stats.sizeHint().width()+32
        self._stats_action.setVisible(self.toolbar.width()>=required)
        if self._menu_buttons:
            self._menu_buttons[-1].setToolTip(tr("Session tools")+" · "+self.stats.text())

    def _action(self, menu, text, callback, *, checkable=False):
        action = QAction(tr(text), self)
        action.setCheckable(checkable)
        action.triggered.connect(callback)
        menu.addAction(action)
        return action

    def _eligible(self):
        return (not self.page._shutdown and self.page._focus_guard()
                and (self.page.isVisible() or self.fullscreen is not None))

    def _frame(self):
        if not self.page.surface.frame_size.isEmpty():
            self._frames += 1
            self._arrivals.append(self.clock())
        self.sync()

    def _state(self, state):
        if state != "Connected":
            self._feedback_text = ""
            self._feedback_timer.stop()
        if state == "Connecting" and self.page.controller.backend is not None:
            # Cert approval also returns to Connecting; reset only a new attempt.
            owner = self.page.controller.owner
            if getattr(self, "_owner", None) != owner:
                self._owner = owner
                self._connected_at = None
                self._elapsed = self._frames = 0
                self._arrivals.clear()
        if state == "Connected" and self._connected_at is None:
            self._connected_at = self.clock()
        elif state != "Connected" and self._connected_at is not None:
            self._elapsed = max(0, self.clock() - self._connected_at)
            self._connected_at = None
        if state != "Connected":
            self.exit_fullscreen()
        if state in {"Ended", "Failed", "Disconnecting"} or state.startswith("Session ended"):
            self.timer.stop()
            self._arrivals.clear()
        elif not self.page._shutdown:
            self.timer.start()
        self.tick()
        self._mode_status()

    def tick(self):
        now = self.clock()
        while self._arrivals and self._arrivals[0] < now - 1:
            self._arrivals.popleft()
        seconds = int(max(0, now - self._connected_at) if self._connected_at is not None
                      else self._elapsed)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours:02}:{minutes:02}:{seconds:02}"
        self.stats.setText(f"{duration} · {len(self._arrivals)} fps")
        self._schedule_statistics_layout()
        self.sync()

    def sync(self):
        connected = self.page.controller.state == "Connected" and not self.page._shutdown
        has_frame = not self.page.surface.frame_size.isEmpty()
        can_input = connected and not self.page.surface.view_only and not self.page.surface.private
        for action in self.shortcut_actions.values():
            action.setEnabled(can_input)
        self.fullscreen_action.setEnabled(connected and self.fullscreen is None)
        self.snapshot_action.setEnabled(connected and has_frame and not self.page.surface.private)
        self.privacy_action.setEnabled(has_frame)
        self.duplicate_action.setEnabled(not self.page._shutdown)
        self.view_only_action.setEnabled(not self.page._shutdown)
        self.other_user_action.setEnabled(
            not self.page._shutdown and not self.page._other_user_pending
            and (connected or not self.page.controller.running)
        )

    def set_view_only(self, enabled):
        self.page.surface.set_view_only(enabled)
        self.view_only_action.setChecked(enabled)
        self._mode_status()
        self.sync()

    def set_private(self, enabled):
        self.page.surface.set_private(enabled)
        self.privacy_action.setChecked(enabled)
        self._mode_status()
        self.sync()

    def _mode_status(self):
        suffix = (tr(" · Screen hidden") if self.page.surface.private else
                  tr(" · View only") if self.page.surface.view_only else
                  tr(" · Keyboard released — click desktop to capture")
                  if self.page.surface.capture_suspended else "")
        feedback = " · " + self._feedback_text if self._feedback_text else ""
        self.page.status.setText(tr(self.page.controller.state) + suffix + feedback)

    def set_keyboard_mode(self, mode):
        self.page.surface.set_keyboard_mode(mode)
        self.keyboard_actions[mode].setChecked(True)

    def _capture_released(self):
        self._mode_status()
        target = (self.fullscreen.findChild(QPushButton, "rdpReturnToTabButton")
                  if self.fullscreen is not None else self.toolbar)
        target.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _feedback(self, text):
        self._feedback_text = text
        self._feedback_timer.start()
        self._mode_status()

    def _clear_feedback(self):
        self._feedback_text = ""
        self._feedback_timer.stop()
        self._mode_status()

    def send_shortcut(self, keys):
        return self._eligible() and self.page.controller.send_shortcut(keys)

    def duplicate(self):
        if self._eligible():
            self.page.duplicate_requested.emit()

    def enter_fullscreen(self):
        if not self._eligible() or self.fullscreen or self.page.controller.state != "Connected":
            return
        self.page.surface.release_input()
        self.page.stack.removeWidget(self.page.desktop_view)
        self.page.stack.setCurrentWidget(self.fullscreen_placeholder)
        self.fullscreen = DesktopFullscreen(self)
        self.page.surface.fullscreen_exit_enabled = True
        self.fullscreen.showFullScreen()
        self.page.surface.setFocus(Qt.FocusReason.OtherFocusReason)
        self.sync()

    def exit_fullscreen(self):
        window, self.fullscreen = self.fullscreen, None
        if window is None:
            return
        self.page.surface.release_input()
        self.page.surface.fullscreen_exit_enabled = False
        window.layout().removeWidget(self.page.desktop_view)
        self.page.stack.insertWidget(0, self.page.desktop_view)
        self.page.stack.setCurrentWidget(self.page.desktop_view)
        window.hide()
        window.deleteLater()
        self.sync()

    def show_details(self):
        if not self._eligible() or self._details is not None:
            return
        controller = self.page.controller
        size = self.page.surface.frame_size
        pid = (int(controller.backend.process.processId()) if controller.backend is not None
               and hasattr(controller.backend, "process") else 0)
        fingerprint = controller.certificate_fingerprint
        text = (f"Server: {controller.host}\nPort: {controller.port}\nState: {controller.state}\n"
                f"Desktop: {size.width()} x {size.height()}\nFrames received: {self._frames}\n"
                f"Session: {self.page.session_id}\nHelper PID: {pid or '—'}\n"
                f"Security: {'NLA' if controller.selected_protocol in (2, 8) else 'Not negotiated'}\n"
                f"Admin session: {controller.options.admin_session}\n"
                f"Dynamic resolution: {controller.options.dynamic_resolution}\n"
                f"Shortcut routing: {self.page.surface.keyboard_mode}\n"
                f"Text clipboard to remote/local: {controller.options.clipboard_to_remote}/"
                f"{controller.options.clipboard_to_local}\n"
                f"Certificate exception: {'This connection only' if controller.certificate_exception else 'None'}\n"
                f"Certificate SHA256: {fingerprint or 'No exception requested; use server certificate administration for details'}")
        dialog = QDialog(self.page.window())
        dialog.setObjectName("rdpConnectionDetailsDialog")
        dialog.setWindowTitle(tr("RDP connection details"))
        root = QVBoxLayout(dialog)
        editor = QPlainTextEdit(dialog)
        editor.setReadOnly(True)
        editor.setPlainText(text)
        root.addWidget(editor)
        footer = QHBoxLayout()
        if fingerprint:
            copy = QPushButton(tr("Copy fingerprint"), dialog)
            copy.setProperty("actionRole", "secondary")
            copy.setAutoDefault(False)
            copy.clicked.connect(lambda: QApplication.clipboard().setText(fingerprint))
            footer.addWidget(copy)
        footer.addStretch()
        close = QPushButton(tr("Close"), dialog)
        close.setObjectName("rdpCloseDetailsButton")
        close.setProperty("actionRole", "secondary")
        close.setAutoDefault(True)
        close.setDefault(True)
        close.clicked.connect(dialog.reject)
        footer.addWidget(close)
        root.addLayout(footer)
        dialog.resize(580, 410)
        fit_dialog_to_screen(dialog)
        self._details = dialog
        try:
            dialog.exec()
        finally:
            self._details = None
            dialog.deleteLater()

    def request_screenshot(self):
        if (not self._eligible() or self._choosing or not self.snapshot_action.isEnabled()
                or self.page.surface.private):
            return
        owner = self.page.controller.owner
        snapshot = self.page.surface.snapshot()
        self._choosing = True
        try:
            filename = self._choose_path()
            if (not filename or not self._eligible() or owner != self.page.controller.owner
                    or self.page.controller.state != "Connected" or self.page.surface.private):
                return
            try:
                save_screenshot(snapshot, filename)
            except (OSError, ValueError):
                self._feedback(tr("Could not save screenshot. Choose a new writable PNG filename."))
            else:
                self._feedback(tr("Screenshot saved. Review it before sharing."))
        finally:
            self._choosing = False

    def _choose_path(self):
        dialog = QFileDialog(self.page.window(), tr("Save remote desktop screenshot"))
        dialog.setObjectName("rdpScreenshotFileDialog")
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter(tr("PNG image (*.png)"))
        dialog.setDefaultSuffix("png")
        dialog.selectFile("rdp-screenshot.png")
        self._chooser = dialog
        try:
            return (dialog.selectedFiles()[0] if dialog.exec() == QDialog.DialogCode.Accepted
                    and dialog.selectedFiles() else "")
        finally:
            self._chooser = None
            dialog.deleteLater()

    def shutdown(self):
        application = QApplication.instance()
        if application is not None:
            application.removeEventFilter(self)
        self.timer.stop()
        self._feedback_timer.stop()
        self.exit_fullscreen()
        for dialog in (self._chooser, self._details):
            if dialog is not None:
                dialog.reject()
