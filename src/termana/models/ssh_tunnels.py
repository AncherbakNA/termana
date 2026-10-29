"""Immutable local TCP forwarding rules, without sockets or credential access."""

from dataclasses import dataclass
from typing import Final

from termana.models.ssh_jump import validate_ssh_route_host

MAX_SSH_LOCAL_FORWARDS: Final = 16


@dataclass(frozen=True, slots=True)
class SshLocalForward:
    """One IPv4-loopback listener forwarding to a host reached from the SSH server."""

    local_port: int
    destination_host: str
    destination_port: int

    def __post_init__(self) -> None:
        for label, port in (
            ("Local tunnel port", self.local_port),
            ("Tunnel destination port", self.destination_port),
        ):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError(f"{label} must be an integer between 1 and 65535")
        # The server resolves this literal; client-side SSH Host aliases do not apply.
        validate_ssh_route_host(self.destination_host)


@dataclass(frozen=True, slots=True)
class SshTunnelOptions:
    """A bounded, ordered collection of distinct local listening ports."""

    local_forwards: tuple[SshLocalForward, ...] = ()

    def __post_init__(self) -> None:
        if type(self.local_forwards) is not tuple:
            raise ValueError("SSH local forwards must be a tuple")
        if len(self.local_forwards) > MAX_SSH_LOCAL_FORWARDS:
            raise ValueError(f"At most {MAX_SSH_LOCAL_FORWARDS} local SSH tunnels are supported")
        ports: set[int] = set()
        for forward in self.local_forwards:
            if type(forward) is not SshLocalForward:
                raise ValueError("SSH local forwards must be validated SshLocalForward values")
            if forward.local_port in ports:
                raise ValueError("Local SSH tunnel ports must be unique within one connection")
            ports.add(forward.local_port)

    @property
    def is_default(self) -> bool:
        return not self.local_forwards
