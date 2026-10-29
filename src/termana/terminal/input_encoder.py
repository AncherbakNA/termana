"""Translate Qt key data into portable VT terminal input sequences."""

import sys

from PySide6.QtCore import Qt

ESCAPE = "\x1b"

NORMAL_CURSOR_KEYS = {
    Qt.Key.Key_Up: f"{ESCAPE}[A",
    Qt.Key.Key_Down: f"{ESCAPE}[B",
    Qt.Key.Key_Right: f"{ESCAPE}[C",
    Qt.Key.Key_Left: f"{ESCAPE}[D",
    Qt.Key.Key_Home: f"{ESCAPE}[H",
    Qt.Key.Key_End: f"{ESCAPE}[F",
}

APPLICATION_CURSOR_KEYS = {
    Qt.Key.Key_Up: f"{ESCAPE}OA",
    Qt.Key.Key_Down: f"{ESCAPE}OB",
    Qt.Key.Key_Right: f"{ESCAPE}OC",
    Qt.Key.Key_Left: f"{ESCAPE}OD",
    Qt.Key.Key_Home: f"{ESCAPE}OH",
    Qt.Key.Key_End: f"{ESCAPE}OF",
}

SPECIAL_KEYS = {
    Qt.Key.Key_Return: "\r",
    Qt.Key.Key_Enter: "\r",
    Qt.Key.Key_Backspace: "\x7f",
    Qt.Key.Key_Tab: "\t",
    Qt.Key.Key_Backtab: f"{ESCAPE}[Z",
    Qt.Key.Key_Escape: ESCAPE,
    Qt.Key.Key_Insert: f"{ESCAPE}[2~",
    Qt.Key.Key_Delete: f"{ESCAPE}[3~",
    Qt.Key.Key_PageUp: f"{ESCAPE}[5~",
    Qt.Key.Key_PageDown: f"{ESCAPE}[6~",
    Qt.Key.Key_F1: f"{ESCAPE}OP",
    Qt.Key.Key_F2: f"{ESCAPE}OQ",
    Qt.Key.Key_F3: f"{ESCAPE}OR",
    Qt.Key.Key_F4: f"{ESCAPE}OS",
    Qt.Key.Key_F5: f"{ESCAPE}[15~",
    Qt.Key.Key_F6: f"{ESCAPE}[17~",
    Qt.Key.Key_F7: f"{ESCAPE}[18~",
    Qt.Key.Key_F8: f"{ESCAPE}[19~",
    Qt.Key.Key_F9: f"{ESCAPE}[20~",
    Qt.Key.Key_F10: f"{ESCAPE}[21~",
    Qt.Key.Key_F11: f"{ESCAPE}[23~",
    Qt.Key.Key_F12: f"{ESCAPE}[24~",
}

MODIFIED_CSI_FINAL_KEYS = {
    Qt.Key.Key_Up: "A",
    Qt.Key.Key_Down: "B",
    Qt.Key.Key_Right: "C",
    Qt.Key.Key_Left: "D",
    Qt.Key.Key_Home: "H",
    Qt.Key.Key_End: "F",
    Qt.Key.Key_F1: "P",
    Qt.Key.Key_F2: "Q",
    Qt.Key.Key_F3: "R",
    Qt.Key.Key_F4: "S",
}

MODIFIED_CSI_TILDE_KEYS = {
    Qt.Key.Key_Insert: 2,
    Qt.Key.Key_Delete: 3,
    Qt.Key.Key_PageUp: 5,
    Qt.Key.Key_PageDown: 6,
    Qt.Key.Key_F5: 15,
    Qt.Key.Key_F6: 17,
    Qt.Key.Key_F7: 18,
    Qt.Key.Key_F8: 19,
    Qt.Key.Key_F9: 20,
    Qt.Key.Key_F10: 21,
    Qt.Key.Key_F11: 23,
    Qt.Key.Key_F12: 24,
}

CONTROL_KEYS = {
    Qt.Key.Key_Space: "\x00",
    Qt.Key.Key_At: "\x00",
    Qt.Key.Key_BracketLeft: "\x1b",
    Qt.Key.Key_Backslash: "\x1c",
    Qt.Key.Key_BracketRight: "\x1d",
    Qt.Key.Key_AsciiCircum: "\x1e",
    Qt.Key.Key_Underscore: "\x1f",
    Qt.Key.Key_Question: "\x7f",
}


class TerminalInputEncoder:
    """Encode keyboard meaning without knowing about widgets or processes."""

    def __init__(
        self,
        *,
        alt_sends_escape: bool = True,
        platform_name: str | None = None,
    ) -> None:
        """Configure whether Alt/Option prefixes printable text with Escape."""
        self.alt_sends_escape = alt_sends_escape
        self.is_macos = (platform_name or sys.platform) == "darwin"

    @property
    def terminal_control_modifier(self) -> Qt.KeyboardModifier:
        """Return Qt's reported modifier for the physical Control key."""
        if self.is_macos:
            return Qt.KeyboardModifier.MetaModifier
        return Qt.KeyboardModifier.ControlModifier

    @property
    def application_modifier(self) -> Qt.KeyboardModifier:
        """Return Qt's reported modifier for Command or the platform Meta key."""
        if self.is_macos:
            return Qt.KeyboardModifier.ControlModifier
        return Qt.KeyboardModifier.MetaModifier

    def encode(
        self,
        key: int,
        text: str,
        modifiers: Qt.KeyboardModifier,
        *,
        application_cursor_keys: bool = False,
        shell_word_navigation: bool = True,
        backspace_sends_ctrl_h: bool = False,
        word_delete_sends_ctrl_w: bool = False,
    ) -> str | None:
        """Return a terminal sequence, or None for an unhandled Qt key."""
        control = bool(modifiers & self.terminal_control_modifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        application_modifier = bool(modifiers & self.application_modifier)
        group_switch = bool(modifiers & Qt.KeyboardModifier.GroupSwitchModifier)

        # Command on macOS and the platform Meta key belong to application
        # shortcuts. They are intentionally not treated as terminal Ctrl.
        if application_modifier:
            return None

        # AltGr is commonly reported as Ctrl+Alt. Printable event text is the
        # reliable signal that the keyboard layout produced a real character.
        if text and (
            group_switch
            or (not self.is_macos and modifiers & Qt.KeyboardModifier.ControlModifier and alt)
        ):
            return text

        if control:
            if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                return chr(key - Qt.Key.Key_A + 1)
            if control_sequence := CONTROL_KEYS.get(key):
                return control_sequence

        # macOS Option editing uses the shell editor's standard Meta bindings.
        # Leave application/alternate-screen and combined modifiers unchanged.
        if (
            self.is_macos and alt and not control and not shift
            and shell_word_navigation and not application_cursor_keys
        ):
            word_operation = {
                Qt.Key.Key_Left: "b",
                Qt.Key.Key_Right: "f",
                Qt.Key.Key_Backspace: "\x7f",
                # macOS Fn+Backspace arrives as forward Delete, not an Fn flag.
                Qt.Key.Key_Delete: "d",
            }.get(key)
            if word_operation is not None:
                if key == Qt.Key.Key_Backspace and word_delete_sends_ctrl_w:
                    return "\x17"
                return ESCAPE + word_operation

        modifier_parameter = 1 + int(shift) + 2 * int(alt) + 4 * int(control)
        if modifier_parameter > 1:
            if final := MODIFIED_CSI_FINAL_KEYS.get(key):
                return f"{ESCAPE}[1;{modifier_parameter}{final}"
            if code := MODIFIED_CSI_TILDE_KEYS.get(key):
                return f"{ESCAPE}[{code};{modifier_parameter}~"

        cursor_keys = APPLICATION_CURSOR_KEYS if application_cursor_keys else NORMAL_CURSOR_KEYS
        if sequence := cursor_keys.get(key):
            return sequence
        if sequence := SPECIAL_KEYS.get(key):
            if key == Qt.Key.Key_Backspace and backspace_sends_ctrl_h:
                return "\x08"
            return sequence
        if not text:
            return None

        if alt and self.alt_sends_escape:
            return ESCAPE + text
        return text
