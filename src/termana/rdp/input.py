"""Attempt-owned input intent; native RDP encoding belongs to the engine adapter."""

from dataclasses import dataclass, field
from enum import StrEnum

from termana.rdp.identity import RdpAttempt, validate_counter


class PointerAction(StrEnum):
    MOVE = "move"
    PRESS = "press"
    RELEASE = "release"
    WHEEL = "wheel"


@dataclass(frozen=True, slots=True)
class RdpKeyInput:
    """Qt key intent, not SSH escape sequences or a portable Windows scancode."""

    owner: RdpAttempt
    pressed: bool = field(repr=False)
    key: int = field(repr=False)
    modifiers: int = field(repr=False)
    native_scan_code: int = field(repr=False)
    native_virtual_key: int = field(repr=False)
    text: str = field(default="", repr=False)
    auto_repeat: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.owner) is not RdpAttempt:
            raise TypeError("RDP input needs an attempt owner")
        if type(self.pressed) is not bool or type(self.auto_repeat) is not bool:
            raise TypeError("Invalid RDP key state")
        for value in (self.key, self.modifiers, self.native_scan_code, self.native_virtual_key):
            if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
                raise ValueError("Invalid RDP key metadata")
        if type(self.text) is not str:
            raise TypeError("RDP key text must be text")
        try:
            size = len(self.text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("Invalid RDP key text") from error
        if size > 4096:
            raise ValueError("RDP key text is too long")


@dataclass(frozen=True, slots=True)
class RdpPointerInput:
    """Remote pixel position; button0 means no button,1/2/3 mean left/right/middle."""

    owner: RdpAttempt
    desktop_generation: int
    action: PointerAction
    x: int = field(repr=False)
    y: int = field(repr=False)
    button: int = field(default=0, repr=False)
    wheel_delta: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if type(self.owner) is not RdpAttempt or type(self.action) is not PointerAction:
            raise TypeError("Invalid RDP pointer identity/action")
        validate_counter(self.desktop_generation, "desktop generation")
        if any(type(value) is not int or not 0 <= value < 8192 for value in (self.x, self.y)):
            raise ValueError("Invalid RDP pointer coordinates")
        if type(self.button) is not int or self.button not in (0, 1, 2, 3):
            raise ValueError("Invalid RDP pointer button")
        if type(self.wheel_delta) is not int or not -32767 <= self.wheel_delta <= 32767:
            raise ValueError("Invalid RDP wheel delta")
        if self.action in (PointerAction.PRESS, PointerAction.RELEASE):
            if not self.button or self.wheel_delta:
                raise ValueError("RDP button events need a button, not wheel input")
        elif self.button or (self.action is PointerAction.MOVE and self.wheel_delta):
            raise ValueError("Invalid RDP pointer event fields")
        if self.action is PointerAction.WHEEL and not self.wheel_delta:
            raise ValueError("RDP wheel events need a delta")


@dataclass(frozen=True, slots=True)
class RdpReleaseAll:
    """Reset helper-held keys/buttons for an attempt, regardless of desktop size.

    The native adapter must track the actual encoded pressed keys/buttons. A
    geometry change must not filter this reset as an old-coordinate event.
    """

    owner: RdpAttempt

    def __post_init__(self) -> None:
        if type(self.owner) is not RdpAttempt:
            raise TypeError("RDP input reset needs an attempt owner")
