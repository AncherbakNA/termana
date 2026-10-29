"""Encode terminal mouse input using xterm's ASCII SGR protocol."""

from enum import IntEnum, auto


class MouseTrackingMode(IntEnum):
    """DEC private modes controlling which mouse events are reported."""

    NORMAL = 1000
    BUTTON_EVENT = 1002
    ANY_EVENT = 1003


class MouseButton(IntEnum):
    """Button values used by xterm's mouse-reporting protocol."""

    LEFT = 0
    MIDDLE = 1
    RIGHT = 2
    NONE = 3
    WHEEL_UP = 64
    WHEEL_DOWN = 65


class MouseEventKind(IntEnum):
    """Logical mouse actions supported by SGR reporting."""

    PRESS = auto()
    RELEASE = auto()
    MOTION = auto()


def encode_sgr_mouse_event(
    button: MouseButton,
    column: int,
    row: int,
    kind: MouseEventKind,
    *,
    shift: bool = False,
    alt: bool = False,
    control: bool = False,
) -> str:
    """Return one xterm SGR mouse report with one-based cell coordinates."""
    if not isinstance(button, MouseButton):
        raise TypeError("button must be MouseButton")
    if not isinstance(kind, MouseEventKind):
        raise TypeError("kind must be MouseEventKind")
    _validate_coordinate(column, name="column")
    _validate_coordinate(row, name="row")
    _validate_modifier(shift, name="shift")
    _validate_modifier(alt, name="alt")
    _validate_modifier(control, name="control")

    if button is MouseButton.NONE and kind is not MouseEventKind.MOTION:
        raise ValueError("MouseButton.NONE is valid only for motion events")
    if button in {MouseButton.WHEEL_UP, MouseButton.WHEEL_DOWN}:
        if kind is not MouseEventKind.PRESS:
            raise ValueError("wheel buttons are valid only for press events")

    button_code = button.value
    if kind is MouseEventKind.MOTION:
        button_code += 32
    button_code += 4 * int(shift) + 8 * int(alt) + 16 * int(control)

    final = "m" if kind is MouseEventKind.RELEASE else "M"
    return f"\x1b[<{button_code};{column};{row}{final}"


def _validate_coordinate(value: int, *, name: str) -> None:
    """Require a positive integer rather than accepting booleans as integers."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _validate_modifier(value: bool, *, name: str) -> None:
    """Reject truthy non-booleans before they alter protocol bits."""
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
