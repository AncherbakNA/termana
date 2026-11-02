"""Canonical loopback forwarding arguments shared by launch and proof isolation."""

from typing import Final

from termana.models.ssh_tunnels import MAX_SSH_LOCAL_FORWARDS, SshLocalForward, SshTunnelOptions

SSH_LOCAL_TUNNEL_POLICY: Final = (
    "-o",
    "GatewayPorts=no",
    "-o",
    "ExitOnForwardFailure=yes",
    "-o",
    "ClearAllForwardings=no",
    "-o",
    # OpenSSH <8.7 has neither this option nor config-driven backgrounding.
    # Ignore only that absent keyword; newer clients still enforce explicit no.
    "IgnoreUnknown=ForkAfterAuthentication",
    "-o",
    "ForkAfterAuthentication=no",
)
_LOOPBACK_PREFIX: Final = "127.0.0.1:"
_MAX_FORWARD_ARGUMENT_LENGTH: Final = len(_LOOPBACK_PREFIX) + 5 + 1 + 253 + 2 + 1 + 5


def ssh_tunnel_arguments(options: SshTunnelOptions) -> tuple[str, ...]:
    """Add rules to SSH config, keep the process owned, and require listener setup."""
    if type(options) is not SshTunnelOptions:
        raise ValueError("SSH tunnels must be validated SshTunnelOptions")
    if options.is_default:
        return ()
    arguments = list(SSH_LOCAL_TUNNEL_POLICY)
    for forward in options.local_forwards:
        host = forward.destination_host
        if ":" in host:
            host = f"[{host}]"
        arguments.extend(
            (
                "-L",
                f"{_LOOPBACK_PREFIX}{forward.local_port}:{host}:{forward.destination_port}",
            )
        )
    return tuple(arguments)


def _port(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 5:
        raise ValueError("Invalid tunnel port")
    port = int(value)
    if str(port) != value:
        raise ValueError("Noncanonical tunnel port")
    return port


def _forward_from_argument(argument: str) -> SshLocalForward:
    if type(argument) is not str:
        raise ValueError("Tunnel arguments must be strings")
    if len(argument) > _MAX_FORWARD_ARGUMENT_LENGTH or not argument.startswith(_LOOPBACK_PREFIX):
        raise ValueError("Unsupported tunnel argument")
    local, separator, destination = argument[len(_LOOPBACK_PREFIX) :].partition(":")
    if not separator:
        raise ValueError("Missing tunnel destination")
    host, separator, remote = destination.rpartition(":")
    if not separator:
        raise ValueError("Missing tunnel destination port")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return SshLocalForward(_port(local), host, _port(remote))


def split_profile_tunnel_arguments(arguments: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    """Remove only an exact builder-produced suffix from a validation copy of argv.

    This does not modify launch arguments. Unknown, partial, reordered, or
    noncanonical rules remain in the copy so the auth bridge refuses to strip
    the original connection isolation. A caller must still validate the prefix.
    """
    cursor = len(arguments)
    reversed_forwards: list[SshLocalForward] = []
    while cursor >= 2 and arguments[cursor - 2] == "-L":
        if len(reversed_forwards) >= MAX_SSH_LOCAL_FORWARDS:
            return arguments, False
        try:
            reversed_forwards.append(_forward_from_argument(arguments[cursor - 1]))
        except ValueError:
            return arguments, False
        cursor -= 2
    if not reversed_forwards or cursor < len(SSH_LOCAL_TUNNEL_POLICY):
        return arguments, False
    start = cursor - len(SSH_LOCAL_TUNNEL_POLICY)
    try:
        options = SshTunnelOptions(tuple(reversed(reversed_forwards)))
    except ValueError:
        return arguments, False
    if ssh_tunnel_arguments(options) != arguments[start:]:
        return arguments, False
    return arguments[:start], True
