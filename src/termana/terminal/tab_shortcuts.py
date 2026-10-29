"""Shared exact shortcuts for terminal tab actions and PTY input gating."""

import sys
from enum import Enum, auto

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QKeySequence


class TerminalTabAction(Enum):
    """Application-owned operations that must never become terminal input."""

    NEW_LOCAL = auto()
    CLOSE_CURRENT = auto()
    NEXT = auto()
    PREVIOUS = auto()


def shortcut_for_terminal_tab_action(
    action: TerminalTabAction,
    *,
    platform_name: str | None = None,
) -> QKeySequence:
    """Return one exact native shortcut without adding unrelated standard bindings."""
    if not isinstance(action, TerminalTabAction):
        raise TypeError("action must be TerminalTabAction")
    selected_platform = sys.platform if platform_name is None else platform_name

    if action in {TerminalTabAction.NEW_LOCAL, TerminalTabAction.CLOSE_CURRENT}:
        # Qt represents macOS Command as ControlModifier, so portable ``Ctrl``
        # sequences render and behave as native Command shortcuts there.
        modifier = "Ctrl" if selected_platform == "darwin" else "Ctrl+Shift"
        key = "T" if action is TerminalTabAction.NEW_LOCAL else "W"
        return QKeySequence(f"{modifier}+{key}")

    # Conversely, Qt represents the physical Control key as MetaModifier on macOS.
    modifier = "Meta" if selected_platform == "darwin" else "Ctrl"
    key = "Shift+Tab" if action is TerminalTabAction.PREVIOUS else "Tab"
    return QKeySequence(f"{modifier}+{key}")


def terminal_tab_action_for_key_event(
    event: QKeyEvent,
    *,
    platform_name: str | None = None,
) -> TerminalTabAction | None:
    """Recognize only reserved tab keys before a terminal can encode them."""
    if not isinstance(event, QKeyEvent):
        raise TypeError("event must be a QKeyEvent")
    selected_platform = sys.platform if platform_name is None else platform_name
    modifiers = event.modifiers() & (
        Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.ShiftModifier
        | Qt.KeyboardModifier.AltModifier
        | Qt.KeyboardModifier.MetaModifier
    )
    key = event.key()

    if selected_platform == "darwin":
        application_modifier = Qt.KeyboardModifier.ControlModifier
        terminal_control_modifier = Qt.KeyboardModifier.MetaModifier
    else:
        application_modifier = (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        )
        terminal_control_modifier = Qt.KeyboardModifier.ControlModifier

    if modifiers == application_modifier:
        if key == Qt.Key.Key_T:
            return TerminalTabAction.NEW_LOCAL
        if key == Qt.Key.Key_W:
            return TerminalTabAction.CLOSE_CURRENT

    if key == Qt.Key.Key_Tab and modifiers == terminal_control_modifier:
        return TerminalTabAction.NEXT
    if key in {Qt.Key.Key_Tab, Qt.Key.Key_Backtab} and modifiers == (
        terminal_control_modifier | Qt.KeyboardModifier.ShiftModifier
    ):
        return TerminalTabAction.PREVIOUS
    return None
