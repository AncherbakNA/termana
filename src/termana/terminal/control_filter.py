"""Bound terminal control sequences before they reach the third-party parser."""

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final

MAX_CSI_SEQUENCE_CHARACTERS: Final = 1024
MAX_OSC_SEQUENCE_CHARACTERS: Final = 4096
MAX_CSI_PARAMETER_COUNT: Final = 64

_ESCAPE: Final = "\x1b"
_CSI_C1: Final = "\x9b"
_OSC_C1: Final = "\x9d"
_STRING_TERMINATOR_C1: Final = "\x9c"
_BELL: Final = "\x07"
_CAN: Final = "\x18"
_SUB: Final = "\x1a"
_CSI_EMBEDDED_CONTROLS: Final = frozenset("\x07\x08\x09\x0a\x0b\x0c\x0d")
_UNSUPPORTED_STRING_ESCAPES: Final = frozenset({"P", "X", "^", "_"})
_UNSUPPORTED_STRING_C1: Final = frozenset({"\x90", "\x98", "\x9e", "\x9f"})
_TEXT_CONTROL_START: Final = re.compile("[\x1b\x90\x98\x9b\x9d\x9e\x9f]")
_CSI_FIXED_PARAMETER_LIMITS: Final = {
    "@": 1,
    "A": 1,
    "B": 1,
    "C": 1,
    "D": 1,
    "E": 1,
    "F": 1,
    "G": 1,
    "H": 2,
    "K": 1,
    "L": 1,
    "M": 1,
    "P": 1,
    "X": 1,
    "`": 1,
    "a": 1,
    "c": 1,
    "d": 1,
    "e": 1,
    "f": 2,
    "g": 1,
    "n": 1,
    "r": 2,
}
_CSI_VARIADIC_FINALS: Final = frozenset({"J", "h", "l", "m"})
_CSI_PRIVATE_FINALS: Final = frozenset({"J", "K", "c", "h", "l"})
_CSI_PYTE_CONTINUATION_CHARACTERS: Final = frozenset(";? >0123456789")
_CSI_ALLOWED_FIRST_PARAMETER_VALUES: Final = {
    "J": frozenset({0, 1, 2, 3}),
    "K": frozenset({0, 1, 2}),
}


@dataclass(frozen=True, slots=True)
class OscTitle:
    """A complete, validated OSC 0/2 title update."""

    value: str


type ControlToken = str | OscTitle


class _State(Enum):
    """Incremental states needed around arbitrary backend chunk boundaries."""

    TEXT = auto()
    TEXT_ESCAPE = auto()
    CSI = auto()
    OSC = auto()
    OSC_ESCAPE = auto()
    IGNORED_STRING = auto()
    IGNORED_STRING_ESCAPE = auto()


class TerminalControlFilter:
    """Expose bounded supported controls and discard unsupported strings safely."""

    def __init__(self) -> None:
        """Create an empty incremental filter."""
        self._state = _State.TEXT
        self._control_characters: list[str] = []
        self._control_limit = 0
        self._control_overflowed = False

    @property
    def buffered_character_count(self) -> int:
        """Return retained control characters for diagnostics and tests."""
        return len(self._control_characters) + int(self._state is _State.TEXT_ESCAPE)

    def feed(self, text: str) -> tuple[ControlToken, ...]:
        """Filter one decoded chunk while preserving event order and fragments."""
        tokens: list[ControlToken] = []
        plain_characters: list[str] = []

        def flush_plain_text() -> None:
            if plain_characters:
                tokens.append("".join(plain_characters))
                plain_characters.clear()

        position = 0
        while position < len(text):
            if self._state is _State.TEXT:
                # Ordinary runs (including C0 controls handled by pyte) do not
                # change this filter's state. Keep the same token boundaries
                # without a Python state-machine iteration for every character.
                control = _TEXT_CONTROL_START.search(text, position)
                end = control.start() if control is not None else len(text)
                if end > position:
                    plain_characters.append(text[position:end])
                    position = end
                if control is None:
                    break
            character = text[position]
            position += 1
            reprocess_as_text = True
            while reprocess_as_text:
                reprocess_as_text = False

                if self._state is _State.TEXT:
                    if character == _ESCAPE:
                        flush_plain_text()
                        self._state = _State.TEXT_ESCAPE
                    elif character == _CSI_C1:
                        flush_plain_text()
                        self._begin_control(_State.CSI, _CSI_C1, MAX_CSI_SEQUENCE_CHARACTERS)
                    elif character == _OSC_C1:
                        flush_plain_text()
                        self._begin_control(_State.OSC, "", MAX_OSC_SEQUENCE_CHARACTERS)
                    elif character in _UNSUPPORTED_STRING_C1:
                        flush_plain_text()
                        self._begin_ignored_string()
                    else:
                        plain_characters.append(character)
                    continue

                if self._state is _State.TEXT_ESCAPE:
                    if character == "[":
                        self._begin_control(
                            _State.CSI,
                            f"{_ESCAPE}[",
                            MAX_CSI_SEQUENCE_CHARACTERS,
                        )
                    elif character == "]":
                        self._begin_control(_State.OSC, "", MAX_OSC_SEQUENCE_CHARACTERS)
                    elif character in _UNSUPPORTED_STRING_ESCAPES:
                        self._begin_ignored_string()
                    elif character == _ESCAPE:
                        # A new ESC restarts the pending escape sequence. Forwarding
                        # both would let the downstream parser reinterpret the second
                        # ESC together with a later CSI/OSC introducer.
                        self._state = _State.TEXT_ESCAPE
                    elif character in {_CAN, _SUB}:
                        self._reset_control()
                    elif character in {
                        _CSI_C1,
                        _OSC_C1,
                        *_UNSUPPORTED_STRING_C1,
                    }:
                        self._reset_control()
                        reprocess_as_text = True
                    else:
                        plain_characters.extend((_ESCAPE, character))
                        self._state = _State.TEXT
                    continue

                if self._state is _State.CSI:
                    if character in _CSI_EMBEDDED_CONTROLS:
                        flush_plain_text()
                        tokens.append(character)
                    elif character in {_CAN, _SUB}:
                        self._reset_control()
                    elif character == _ESCAPE:
                        self._reset_control()
                        self._state = _State.TEXT_ESCAPE
                    elif 0x40 <= ord(character) <= 0x7E:
                        self._append_control(character)
                        if not self._control_overflowed and self._csi_is_safe_to_forward():
                            tokens.append("".join(self._control_characters))
                        self._reset_control()
                    elif 0x20 <= ord(character) <= 0x3F:
                        self._append_control(character)
                    else:
                        self._reset_control()
                        reprocess_as_text = True
                    continue

                if self._state is _State.OSC_ESCAPE:
                    if character in {_CAN, _SUB}:
                        self._reset_control()
                    elif character == "\\":
                        self._finish_osc(tokens)
                    else:
                        self._append_control(_ESCAPE)
                        self._append_control(character)
                        self._state = _State.OSC
                    continue

                if self._state is _State.OSC:
                    if character in {_CAN, _SUB}:
                        self._reset_control()
                    elif character in {_BELL, _STRING_TERMINATOR_C1}:
                        self._finish_osc(tokens)
                    elif character == _ESCAPE:
                        self._state = _State.OSC_ESCAPE
                    else:
                        self._append_control(character)
                    continue

                if self._state is _State.IGNORED_STRING_ESCAPE:
                    if character in {_CAN, _SUB}:
                        self._reset_control()
                    elif character == "\\":
                        self._reset_control()
                    else:
                        self._state = _State.IGNORED_STRING
                    continue

                if character in {_CAN, _SUB, _STRING_TERMINATOR_C1}:
                    self._reset_control()
                elif character == _ESCAPE:
                    self._state = _State.IGNORED_STRING_ESCAPE

        flush_plain_text()
        return tuple(tokens)

    def finish(self) -> None:
        """Discard any incomplete terminal control at end of process."""
        self._reset_control()

    def _begin_control(self, state: _State, prefix: str, limit: int) -> None:
        """Start one bounded CSI or OSC payload."""
        self._state = state
        self._control_characters = list(prefix)
        self._control_limit = limit
        self._control_overflowed = False

    def _begin_ignored_string(self) -> None:
        """Discard a DCS/SOS/PM/APC payload without retaining its contents."""
        self._state = _State.IGNORED_STRING
        self._control_characters.clear()
        self._control_limit = 0
        self._control_overflowed = False

    def _append_control(self, character: str) -> None:
        """Retain a character only while the sequence remains within budget."""
        if self._control_overflowed:
            return
        if len(self._control_characters) >= self._control_limit:
            self._control_characters.clear()
            self._control_overflowed = True
            return
        self._control_characters.append(character)

    def _finish_osc(self, tokens: list[ControlToken]) -> None:
        """Publish only exact supported numeric commands after their terminator."""
        if not self._control_overflowed:
            command = "".join(self._control_characters)
            code_text, separator, value = command.partition(";")
            if (
                separator
                and code_text
                and code_text.isascii()
                and code_text.isdecimal()
                and int(code_text) in {0, 2}
            ):
                tokens.append(OscTitle(value))
        self._reset_control()

    def _csi_is_safe_to_forward(self) -> bool:
        """Reject parameter shapes that would call pyte handlers incorrectly."""
        prefix_length = 2 if self._control_characters[:2] == [_ESCAPE, "["] else 1
        parameter_characters = self._control_characters[prefix_length:-1]
        final_character = self._control_characters[-1]

        # Other CSI intermediate bytes make pyte stop at its safe debug handler
        # before it reaches the final byte, so no application handler is called.
        if any(
            character not in _CSI_PYTE_CONTINUATION_CHARACTERS for character in parameter_characters
        ):
            return True

        parameter_count = parameter_characters.count(";") + 1
        if parameter_count > MAX_CSI_PARAMETER_COUNT:
            return False

        numeric_parameters = "".join(
            character for character in parameter_characters if character not in "? >"
        )
        first_parameter_text = numeric_parameters.partition(";")[0]
        first_parameter = min(int(first_parameter_text or 0), 9999)
        allowed_first_parameters = _CSI_ALLOWED_FIRST_PARAMETER_VALUES.get(final_character)
        if allowed_first_parameters is not None and first_parameter not in allowed_first_parameters:
            return False

        if "?" in parameter_characters and final_character not in _CSI_PRIVATE_FINALS:
            return False
        if final_character in _CSI_VARIADIC_FINALS:
            return True
        parameter_limit = _CSI_FIXED_PARAMETER_LIMITS.get(final_character)
        return parameter_limit is None or parameter_count <= parameter_limit

    def _reset_control(self) -> None:
        """Return to ordinary text without retaining untrusted payload data."""
        self._state = _State.TEXT
        self._control_characters.clear()
        self._control_limit = 0
        self._control_overflowed = False
