"""Embedded desktop view; transport, trust and native key mapping live elsewhere."""

from dataclasses import replace
from uuid import UUID

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QThread, Signal
from PySide6.QtGui import QImage, QKeyEvent, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from termana.i18n import tr
from termana.models.rdp_options import RDP_KEYBOARD_MODES
from termana.rdp.frame import RdpFrame, ViewportTransform
from termana.rdp.identity import RdpAttempt, validate_counter
from termana.rdp.input import PointerAction, RdpKeyInput, RdpPointerInput, RdpReleaseAll
from termana.terminal.tab_shortcuts import terminal_tab_action_for_key_event

_BUTTONS = {
    Qt.MouseButton.LeftButton: 1,
    Qt.MouseButton.RightButton: 2,
    Qt.MouseButton.MiddleButton: 3,
}
_MAX_HELD_KEYS = 128


def _key_identity(key, scan, virtual):
    # Releasing Shift before a punctuation key changes Qt's logical key.
    # Physical identity must still release the exact original held intent.
    return (0, scan, 0) if scan else ((0, 0, virtual) if virtual else (key, 0, 0))


class RdpSurface(QWidget):
    """Own a display copy and gate input; this widget does not establish RDP."""

    remote_input = Signal(object)
    frame_changed = Signal()
    exit_fullscreen_requested = Signal()
    eligibility_changed = Signal()
    capture_released = Signal()

    def __init__(self, session_id: UUID, parent=None):
        if type(session_id) is not UUID:
            raise TypeError("RDP surface needs a stable session UUID")
        super().__init__(parent)
        # Initialize before setters can dispatch Qt events into this subclass.
        self._session_id = session_id
        self._owner = None
        self._accept_frames = False
        self._connected = False
        self._input_enabled = False
        self._desktop_generation = 0
        self._frame_sequence = 0
        self._image = QImage()
        self._held_keys = {}
        self._held_buttons = set()
        self._modifiers_observed = 0
        self._view_only = False
        self._private = False
        self.fullscreen_exit_enabled = False
        self.keyboard_mode = "remote"
        self.capture_suspended = False
        self.setObjectName("rdpDesktopSurface")
        self.setAccessibleName(tr("Remote desktop"))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        application = QApplication.instance()
        if application is not None:
            # X11 may keep the parent active when an application-modal dialog
            # blocks it. Observe the top-level WindowBlocked event, including
            # when this surface is nested inside a workspace pane. QObject
            # automatically removes this filter when the surface is destroyed.
            application.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type()==QEvent.Type.WindowBlocked and watched is self.window():
            self.release_input()
            self.eligibility_changed.emit()
        return False

    def _assert_gui_thread(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("RDP surface must be updated on its GUI thread")

    @property
    def session_id(self):
        return self._session_id

    @property
    def owner(self):
        return self._owner

    @property
    def desktop_generation(self):
        return self._desktop_generation

    def _eligible(self, *, require_focus=True):
        return (
            self._accept_frames and self._connected and self._input_enabled
            and not self._view_only and not self._private
            and not self._image.isNull() and self.isEnabled() and self.isVisible()
            and self.isActiveWindow() and QApplication.activeModalWidget() is None
            and (not require_focus or self.hasFocus())
        )

    @property
    def interactive(self):
        return not self.capture_suspended and self._eligible()

    def set_keyboard_mode(self, mode):
        if mode not in RDP_KEYBOARD_MODES:
            raise ValueError("Unknown RDP shortcut policy")
        self.release_input()
        self.keyboard_mode = mode

    def release_capture(self):
        self.release_input()
        self.capture_suspended = True
        self.eligibility_changed.emit()
        self.capture_released.emit()

    def capture_keyboard(self):
        if self._eligible(require_focus=False):
            self.capture_suspended = False
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            self.eligibility_changed.emit()

    def _local_shortcut(self, event):
        remote = self.keyboard_mode == "remote" or (
            self.keyboard_mode == "fullscreen" and self.fullscreen_exit_enabled)
        modified = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                        | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier))
        return not remote and (modified or Qt.Key.Key_F1 <= event.key() <= Qt.Key.Key_F35)

    @property
    def actions_allowed(self):
        return self._eligible(require_focus=False)

    @property
    def frame_size(self):
        return QSize(self._image.size())

    @property
    def view_only(self):
        return self._view_only

    @property
    def private(self):
        return self._private

    def snapshot(self):
        """Explicit current desktop only; never trust/login UI or hidden pixels."""
        return QImage() if self._private else self._image.copy()

    def release_input(self):
        self._emit_reset(self._take_reset())

    def set_view_only(self, enabled):
        if type(enabled) is not bool:
            raise TypeError("View-only mode must be boolean")
        self._view_only = enabled
        if enabled:
            self.release_input()
        self.eligibility_changed.emit()

    def set_private(self, enabled):
        if type(enabled) is not bool:
            raise TypeError("Privacy mode must be boolean")
        self._private = enabled
        if enabled:
            self.release_input()
            self.unsetCursor()
        self.update()
        self.eligibility_changed.emit()

    def begin_attempt(self, owner: RdpAttempt):
        self._assert_gui_thread()
        if type(owner) is not RdpAttempt or owner.session_id != self._session_id:
            raise ValueError("RDP attempt does not belong to this tab")
        if self._owner is not None and owner.attempt_id == self._owner.attempt_id:
            raise ValueError("RDP reconnect needs a fresh attempt UUID")
        reset = self._take_reset()
        self._owner = owner
        self._connected = self._input_enabled = False
        self._accept_frames = True
        self._desktop_generation = self._frame_sequence = 0
        self._image = QImage()
        self.capture_suspended = False
        self.update()
        # Commit before emitting: a synchronous receiver may reconnect/close.
        self._emit_reset(reset)
        self.frame_changed.emit()

    def submit_frame(self, owner: RdpAttempt, generation: int, sequence: int,
                     frame: RdpFrame) -> bool:
        self._assert_gui_thread()
        if owner != self._owner or not self._accept_frames:
            return False
        validate_counter(generation, "desktop generation")
        validate_counter(sequence, "frame sequence")
        if generation < self._desktop_generation or sequence <= self._frame_sequence:
            return False
        if type(frame) is not RdpFrame:
            raise TypeError("Expected a validated RDP frame")
        changed = generation != self._desktop_generation
        if not changed and (frame.width, frame.height) != (
            self._image.width(), self._image.height()
        ):
            raise ValueError("RDP geometry changed without a desktop generation")
        image = QImage(frame.pixels, frame.width, frame.height, frame.stride,
                       QImage.Format.Format_RGBX8888).copy()
        if image.isNull():
            raise ValueError("Could not allocate RDP display frame")
        reset = self._take_reset() if changed else None
        self._image = image
        self._desktop_generation = generation
        self._frame_sequence = sequence
        self.update()
        self._emit_reset(reset)
        self.frame_changed.emit()
        return True

    def set_connected(self, owner: RdpAttempt, connected: bool) -> bool:
        self._assert_gui_thread()
        if type(connected) is not bool:
            raise TypeError("RDP connection state must be a bool")
        if owner != self._owner or not self._accept_frames:
            return False
        reset = self._take_reset() if not connected else None
        self._connected = connected
        if not connected:
            self._input_enabled = False
        self._emit_reset(reset)
        self.eligibility_changed.emit()
        return True

    def set_input_enabled(self, owner: RdpAttempt, enabled: bool) -> bool:
        """Controller grants active-pane/capture ownership; focus is also required."""
        self._assert_gui_thread()
        if type(enabled) is not bool:
            raise TypeError("RDP input ownership must be a bool")
        if owner != self._owner or not self._accept_frames:
            return False
        reset = self._take_reset() if not enabled else None
        self._input_enabled = enabled
        self._emit_reset(reset)
        self.eligibility_changed.emit()
        return True

    def end_attempt(self, owner: RdpAttempt) -> bool:
        self._assert_gui_thread()
        if owner != self._owner or not self._accept_frames:
            return False
        reset = self._take_reset()
        self._connected = self._input_enabled = self._accept_frames = False
        self._emit_reset(reset)
        self.eligibility_changed.emit()
        # Preserve the last image, but reject late frames and credential/input use.
        return True

    def _transform(self):
        return ViewportTransform(self._image.width(), self._image.height(),
                                 self.width(), self.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().brush(QPalette.ColorRole.Base))
        if self._private or self._image.isNull():
            painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             tr("Screen hidden locally · connection stays open") if self._private
                             else tr("Remote desktop has not started"))
        else:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(QRectF(*self._transform().rectangle), self._image)

    def event(self, event):
        # QWidget construction can dispatch events before Python fields exist.
        if not hasattr(self, "_owner"):
            return super().event(event)
        if isinstance(event, QKeyEvent):
            release = (event.key() == Qt.Key.Key_F12 and event.modifiers() == (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
            if release:
                event.accept()
                if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                    self.release_capture()
                return True
            local_exit = (self.fullscreen_exit_enabled and event.key() == Qt.Key.Key_F11
                          and event.modifiers() == (Qt.KeyboardModifier.ControlModifier
                                                    | Qt.KeyboardModifier.ShiftModifier))
            if local_exit:
                event.accept()
                if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                    self.release_input()
                    self.exit_fullscreen_requested.emit()
                return True
            reserved = terminal_tab_action_for_key_event(event) is not None
            if reserved and event.type() == QEvent.Type.ShortcutOverride:
                event.ignore()
                return False
            if reserved and event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                event.accept()
                return True
            if self._local_shortcut(event):
                # A formerly forwarded key must be released if its route changes.
                if event.type() == QEvent.Type.KeyPress:
                    self.release_input()
                event.ignore()
                return False
            if event.type() == QEvent.Type.ShortcutOverride and self.interactive:
                event.accept()
                return True
        if event.type() == QEvent.Type.WindowDeactivate or (
            event.type() == QEvent.Type.EnabledChange and not self.isEnabled()
        ):
            self._emit_reset(self._take_reset())
        if event.type() in {QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate,
                            QEvent.Type.EnabledChange, QEvent.Type.Show, QEvent.Type.Hide,
                            QEvent.Type.FocusIn, QEvent.Type.FocusOut}:
            self.eligibility_changed.emit()
        return super().event(event)

    def focusNextPrevChild(self, next):
        return False if self.interactive else super().focusNextPrevChild(next)

    def keyPressEvent(self, event):
        if not self.interactive or self._local_shortcut(event):
            event.ignore()
            return
        key = RdpKeyInput(self._owner, True, event.key(), event.modifiers().value,
                          event.nativeScanCode(), event.nativeVirtualKey(), event.text(),
                          event.isAutoRepeat())
        identity = _key_identity(key.key, key.native_scan_code, key.native_virtual_key)
        self._modifiers_observed = event.modifiers().value
        if identity in self._held_keys or len(self._held_keys) < _MAX_HELD_KEYS:
            self._held_keys[identity] = key
            self.remote_input.emit(key)
        event.accept()

    def keyReleaseEvent(self, event):
        identity = _key_identity(event.key(), event.nativeScanCode(), event.nativeVirtualKey())
        key = self._held_keys.get(identity)
        if self.interactive and key is not None and not event.isAutoRepeat():
            del self._held_keys[identity]
            self._modifiers_observed = event.modifiers().value
            self.remote_input.emit(replace(key, pressed=False, text="", auto_repeat=False,
                                           modifiers=event.modifiers().value))
        event.accept()

    def _pointer(self, event, action, button=0, wheel_delta=0):
        if action is PointerAction.PRESS and self._eligible(require_focus=False):
            self.capture_keyboard()
        if not self.interactive:
            event.ignore()
            return
        point = self._transform().remote_point(event.position().x(), event.position().y(),
                                               clamp=bool(self._held_buttons))
        if point is None:
            event.ignore()
            return
        if action is PointerAction.PRESS:
            self._held_buttons.add(button)
        elif action is PointerAction.RELEASE:
            if button not in self._held_buttons:
                event.accept()
                return
            self._held_buttons.remove(button)
        self.remote_input.emit(RdpPointerInput(self._owner, self._desktop_generation,
                                              action, *point, button, wheel_delta))
        event.accept()

    def mouseMoveEvent(self, event):
        self._pointer(event, PointerAction.MOVE)

    def mousePressEvent(self, event):
        if button := _BUTTONS.get(event.button()):
            self._pointer(event, PointerAction.PRESS, button)
        else:
            event.ignore()

    def mouseReleaseEvent(self, event):
        if button := _BUTTONS.get(event.button()):
            self._pointer(event, PointerAction.RELEASE, button)
        else:
            event.ignore()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self._pointer(event, PointerAction.WHEEL,
                          wheel_delta=max(-32767, min(32767, delta)))
        else:
            event.ignore()

    def _take_reset(self):
        held = bool(self._held_keys or self._held_buttons or self._modifiers_observed)
        self._modifiers_observed = 0
        self._held_keys.clear()
        self._held_buttons.clear()
        return RdpReleaseAll(self._owner) if held else None

    def _emit_reset(self, reset):
        if reset is not None:
            self.remote_input.emit(reset)

    def focusOutEvent(self, event):
        self._emit_reset(self._take_reset())
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self._emit_reset(self._take_reset())
        super().hideEvent(event)

    def closeEvent(self, event):
        if self._owner is not None:
            self.end_attempt(self._owner)
        super().closeEvent(event)
