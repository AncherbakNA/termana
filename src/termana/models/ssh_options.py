"""Bounded, explicit SSH connection timeout and keepalive overrides."""

from dataclasses import dataclass
from typing import Final

MAX_SSH_CONNECT_TIMEOUT: Final = 600
MAX_SSH_SERVER_ALIVE_INTERVAL: Final = 3600
MAX_SSH_SERVER_ALIVE_COUNT: Final = 100


@dataclass(frozen=True, slots=True)
class SshConnectionOptions:
    """Portable overrides; None leaves the corresponding OpenSSH default untouched."""

    connect_timeout: int | None = None
    server_alive_interval: int | None = None
    server_alive_count_max: int | None = None

    def __post_init__(self) -> None:
        """Reject coercions, unbounded timers, and inconsistent keepalive settings."""
        for field_name, minimum, maximum in (
            ("connect_timeout", 1, MAX_SSH_CONNECT_TIMEOUT),
            ("server_alive_interval", 0, MAX_SSH_SERVER_ALIVE_INTERVAL),
            ("server_alive_count_max", 1, MAX_SSH_SERVER_ALIVE_COUNT),
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            if type(value) is not int:
                raise ValueError(f"SSH {field_name} must be an integer or None")
            if not minimum <= value <= maximum:
                raise ValueError(f"SSH {field_name} must be between {minimum} and {maximum}")
        if self.server_alive_interval is None or self.server_alive_interval == 0:
            if self.server_alive_count_max is not None:
                raise ValueError("SSH keepalive count requires a positive server_alive_interval")
        elif self.server_alive_count_max is None:
            raise ValueError("A positive SSH server_alive_interval requires a keepalive count")

    @property
    def is_default(self) -> bool:
        """Distinguish untouched defaults from explicitly disabled keepalives."""
        return (
            self.connect_timeout is None
            and self.server_alive_interval is None
            and self.server_alive_count_max is None
        )
