"""Bounded single-hop SSH routing safe for OpenSSH's internal proxy command."""

import re
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv6Address
from typing import Final

MAX_SSH_JUMP_HOST_CHARACTERS: Final = 253
MAX_SSH_JUMP_USERNAME_CHARACTERS: Final = 128
_ROUTE_HOST_PATTERN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_ROUTE_USERNAME_PATTERN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")


class SshJumpMode(StrEnum):
    """Inherit configuration, connect directly, or select one explicit gateway."""

    CONFIG = "config"
    DIRECT = "direct"
    JUMP = "jump"


def validate_ssh_route_host(host: object) -> str:
    """Accept a literal ASCII host/alias or unbracketed IPv6 without expansion syntax."""
    if type(host) is not str:
        raise ValueError("SSH route host must be a string")
    if not 1 <= len(host) <= MAX_SSH_JUMP_HOST_CHARACTERS:
        raise ValueError("SSH route host must contain between 1 and 253 characters")
    if _ROUTE_HOST_PATTERN.fullmatch(host) is not None:
        return host
    if host.isascii() and ":" in host and "%" not in host:
        try:
            IPv6Address(host)
        except ValueError:
            pass
        else:
            return host
    raise ValueError(
        "SSH route host must be a safe ASCII hostname, alias, or unbracketed IPv6 address"
    )


@dataclass(frozen=True, slots=True)
class SshJumpOptions:
    """Portable routing choices; these contain no gateway secrets or key paths."""

    mode: SshJumpMode = SshJumpMode.CONFIG
    host: str | None = None
    port: int | None = None
    username: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous routing and shell/expansion syntax without DNS or file access."""
        if type(self.mode) is not SshJumpMode:
            raise ValueError("SSH jump mode must be SshJumpMode")
        if self.mode is not SshJumpMode.JUMP:
            if self.host is not None or self.port is not None or self.username is not None:
                raise ValueError("SSH config and direct modes cannot include jump host fields")
            return
        host = validate_ssh_route_host(self.host)
        if host.casefold() == "none":
            raise ValueError("SSH jump host cannot use the reserved name none")
        if self.port is not None:
            if type(self.port) is not int:
                raise ValueError("SSH jump port must be an integer or None")
            if not 1 <= self.port <= 65535:
                raise ValueError("SSH jump port must be between 1 and 65535")
        if self.username is not None:
            if type(self.username) is not str:
                raise ValueError("SSH jump username must be a string or None")
            if not 1 <= len(self.username) <= MAX_SSH_JUMP_USERNAME_CHARACTERS:
                raise ValueError("SSH jump username must contain between 1 and 128 characters")
            if _ROUTE_USERNAME_PATTERN.fullmatch(self.username) is None:
                raise ValueError(
                    "SSH jump username must contain only safe ASCII username characters"
                )

    @property
    def is_default(self) -> bool:
        """Only config mode leaves the route inherited from OpenSSH configuration."""
        return self.mode is SshJumpMode.CONFIG

    @property
    def proxy_jump_value(self) -> str:
        """Format one validated gateway for -J, without expanding or normalizing values."""
        if self.mode is not SshJumpMode.JUMP:
            raise ValueError("An explicit SSH jump host is required")
        host = f"[{self.host}]" if ":" in self.host else self.host
        user_prefix = "" if self.username is None else f"{self.username}@"
        port_suffix = "" if self.port is None else f":{self.port}"
        return f"{user_prefix}{host}{port_suffix}"
