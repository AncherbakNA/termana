"""Qt logical US keys → Windows virtual keys; WinPR performs scancode mapping."""

import sys

from PySide6.QtCore import Qt

from termana.rdp.input import PointerAction

_KEYS = {
    Qt.Key.Key_Escape: 0x1B, Qt.Key.Key_Tab: 9, Qt.Key.Key_Backtab: 9,
    Qt.Key.Key_Backspace: 8, Qt.Key.Key_Return: 13, Qt.Key.Key_Enter: 13,
    Qt.Key.Key_Insert: 0x2D, Qt.Key.Key_Delete: 0x2E, Qt.Key.Key_Home: 0x24,
    Qt.Key.Key_End: 0x23, Qt.Key.Key_Left: 0x25, Qt.Key.Key_Up: 0x26,
    Qt.Key.Key_Right: 0x27, Qt.Key.Key_Down: 0x28, Qt.Key.Key_PageUp: 0x21,
    Qt.Key.Key_PageDown: 0x22, Qt.Key.Key_Shift: 0x10, Qt.Key.Key_Alt: 0x12,
    Qt.Key.Key_CapsLock: 0x14, Qt.Key.Key_Space: 0x20,
    Qt.Key.Key_Control: 0x11, Qt.Key.Key_Meta: 0x5B,
}
for plain, shifted, virtual in (
    (";", ":", 0xBA), ("=", "+", 0xBB), (",", "<", 0xBC),
    ("-", "_", 0xBD), (".", ">", 0xBE), ("/", "?", 0xBF),
    ("`", "~", 0xC0), ("[", "{", 0xDB), ("\\", "|", 0xDC),
    ("]", "}", 0xDD), ("'", '"', 0xDE),
):
    _KEYS[ord(plain)] = _KEYS[ord(shifted)] = virtual
for shifted, number in zip("!@#$%^&*()", "1234567890", strict=True):
    _KEYS[ord(shifted)] = ord(number)


def virtual_key(key: int, *, platform: str = sys.platform) -> int | None:
    if ord("A") <= key <= ord("Z") or ord("0") <= key <= ord("9"):
        return key
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
        return 0x70 + key - Qt.Key.Key_F1
    if platform == "darwin":
        # Qt swaps the names Control/Meta on Cocoa: preserve physical Control
        # as Windows Ctrl and physical Command as Windows, not the reverse.
        if key == Qt.Key.Key_Control:
            return 0x5B
        if key == Qt.Key.Key_Meta:
            return 0x11
    return _KEYS.get(key)


def pointer_flags(event) -> int:
    if event.action is PointerAction.MOVE:
        return 0x0800
    if event.action is PointerAction.WHEEL:
        return 0x0200 | (event.wheel_delta & 0x1FF)
    mask = {1: 0x1000, 2: 0x2000, 3: 0x4000}[event.button]
    return mask | (0x8000 if event.action is PointerAction.PRESS else 0)


def modifier_keys(modifiers: int, *, platform: str = sys.platform) -> set[int]:
    """Reconcile modifier snapshots, including Qt-generated/accessibility events."""
    mapping = {Qt.KeyboardModifier.ShiftModifier.value: 0x10,
               Qt.KeyboardModifier.AltModifier.value: 0x12,
               Qt.KeyboardModifier.ControlModifier.value: 0x5B if platform == "darwin" else 0x11,
               Qt.KeyboardModifier.MetaModifier.value: 0x11 if platform == "darwin" else 0x5B}
    return {virtual for flag, virtual in mapping.items() if modifiers & flag}
