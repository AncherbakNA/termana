"""Stable tab identity and fresh transport ownership; no connection or secrets."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RdpAttempt:
    """A reconnect changes attempt_id, never the containing tab's session_id."""

    session_id: UUID
    attempt_id: UUID

    def __post_init__(self) -> None:
        if type(self.session_id) is not UUID or type(self.attempt_id) is not UUID:
            raise TypeError("RDP ownership requires session and attempt UUIDs")


def validate_counter(value: int, name: str) -> None:
    """Counters are positive bounded wire values, not booleans or float IDs."""
    if type(value) is not int or not 1 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"Invalid RDP {name}")
