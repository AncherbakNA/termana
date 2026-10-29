"""Small, immutable display metadata for already-open terminal sessions."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from termana.models.terminal_layout import MAX_TERMINAL_PANES
from termana.terminal.title import sanitize_terminal_title


class OpenTerminalState(StrEnum):
    """Application-owned state labels, never derived from terminal output."""

    OPEN = "Open"
    ENDED = "Ended"
    LOGIN = "Login required"
    PASSWORD = "Password required"
    AUTHENTICATION = "SSH authentication"
    RECONNECTING = "Reconnecting"


@dataclass(frozen=True, slots=True)
class OpenTerminalSession:
    """A read-only session identity and bounded, non-secret presentation fields."""

    session_id: UUID
    title: str
    session_name: str
    pane_number: int
    state: OpenTerminalState
    active: bool = False
    attention: bool = False

    def __post_init__(self) -> None:
        if type(self.session_id) is not UUID:
            raise TypeError("session_id must be a UUID")
        for field_name in ("title", "session_name"):
            value = getattr(self, field_name)
            if type(value) is not str:
                raise TypeError(f"{field_name} must be a string")
            normalized = sanitize_terminal_title(value)
            if not normalized:
                raise ValueError(f"{field_name} must contain displayable text")
            object.__setattr__(self, field_name, normalized)
        if type(self.pane_number) is not int:
            raise TypeError("pane_number must be an int")
        if not 1 <= self.pane_number <= MAX_TERMINAL_PANES:
            raise ValueError(f"pane_number must be between 1 and {MAX_TERMINAL_PANES}")
        if type(self.state) is not OpenTerminalState:
            raise TypeError("state must be OpenTerminalState")
        if type(self.active) is not bool or type(self.attention) is not bool:
            raise TypeError("active and attention must be booleans")
