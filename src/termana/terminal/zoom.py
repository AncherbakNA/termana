"""Shared terminal zoom shortcuts for window actions and terminal input."""

from enum import Enum, auto

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QKeySequence


class ZoomAction(Enum):
    """Application-owned changes to the terminal font point size."""

    INCREASE = auto()
    DECREASE = auto()
    RESET = auto()


def shortcuts_for_zoom_action(action: ZoomAction) -> tuple[QKeySequence, ...]:
    """Return native application shortcuts plus the common unshifted alias."""
    if not isinstance(action, ZoomAction):
        raise TypeError("action must be ZoomAction")
    if action is ZoomAction.INCREASE:
        shortcuts = [
            *QKeySequence.keyBindings(QKeySequence.StandardKey.ZoomIn),
            QKeySequence("Ctrl+="),
            # Qt may report the shifted physical key either as Plus or as
            # Equal with Shift still present. Keep both exact event forms
            # backed by a real QAction shortcut before TerminalView yields.
            QKeySequence("Ctrl+Shift++"),
            QKeySequence("Ctrl+Shift+="),
        ]
    elif action is ZoomAction.DECREASE:
        shortcuts = QKeySequence.keyBindings(QKeySequence.StandardKey.ZoomOut)
    else:
        shortcuts = [QKeySequence("Ctrl+0")]

    unique_shortcuts: list[QKeySequence] = []
    for shortcut in shortcuts:
        if shortcut not in unique_shortcuts:
            unique_shortcuts.append(shortcut)
    return tuple(unique_shortcuts)


def zoom_action_for_key_event(event: QKeyEvent) -> ZoomAction | None:
    """Recognize application zoom keys before they become terminal bytes."""
    if not isinstance(event, QKeyEvent):
        raise TypeError("event must be a QKeyEvent")

    relevant_modifiers = event.modifiers() & (
        Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.ShiftModifier
        | Qt.KeyboardModifier.AltModifier
        | Qt.KeyboardModifier.MetaModifier
    )
    application_modifier = Qt.KeyboardModifier.ControlModifier
    key = event.key()

    if key == Qt.Key.Key_ZoomIn and relevant_modifiers == Qt.KeyboardModifier.NoModifier:
        return ZoomAction.INCREASE
    if key == Qt.Key.Key_ZoomOut and relevant_modifiers == Qt.KeyboardModifier.NoModifier:
        return ZoomAction.DECREASE
    if key in {Qt.Key.Key_Plus, Qt.Key.Key_Equal} and relevant_modifiers in {
        application_modifier,
        application_modifier | Qt.KeyboardModifier.ShiftModifier,
    }:
        return ZoomAction.INCREASE
    if key == Qt.Key.Key_Minus and relevant_modifiers == application_modifier:
        return ZoomAction.DECREASE
    if key == Qt.Key.Key_0 and relevant_modifiers == application_modifier:
        return ZoomAction.RESET
    return None
