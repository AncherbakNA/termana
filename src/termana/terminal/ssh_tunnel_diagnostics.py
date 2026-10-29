"""Bounded, advisory-only interpretations of SSH forwarding diagnostics.

Output can include remote text, especially through a gateway. These hints never
prove a listener's state, authorize recovery, or copy remote reasons into the UI.
"""

import re
from enum import StrEnum

from termana.models.ssh_tunnels import SshTunnelOptions

MAX_FORWARD_DIAGNOSTIC_BYTES = 64 * 1024
_CHANNEL_FAILURE = re.compile(rb"(?:^|: )channel [0-9]{1,10}: open failed:")
_BIND_FAILURE = re.compile(
    rb"(?:error: )?bind \[127\.0\.0\.1\]:([0-9]{1,5}): (Address already in use|Permission denied)"
)
# ConPTY can wrap a complete record in erase-to-end-of-line commands.
# Do not strip arbitrary ANSI (colors, cursor movement, OSC/DCS payloads): this
# is only an advisory fatal-record match, never authentication or listener proof.
_FORWARD_FATAL = re.compile(
    rb"(?:\x1b\[K)*(?:ssh: )?Could not request local forwarding\.(?:\x1b\[K)*"
)


class SshChannelFailure(StrEnum):
    GENERIC = "generic"
    CONNECT_FAILED = "connect_failed"
    PROHIBITED = "prohibited"


def channel_failure_kind(line: bytes) -> SshChannelFailure | None:
    """Classify a bounded line without retaining any server-provided reason."""
    if len(line) > 16 * 1024:
        return None
    match = _CHANNEL_FAILURE.search(line)
    if match is None:
        return None
    reason = line[match.end() : match.end() + 40]
    if reason.startswith(b" connect failed:"):
        return SshChannelFailure.CONNECT_FAILED
    if reason.startswith(b" administratively prohibited:"):
        return SshChannelFailure.PROHIBITED
    return SshChannelFailure.GENERIC


def channel_failure_guidance(kind: SshChannelFailure) -> str:
    """Explain the category without attributing an unidentifiable channel to a rule."""
    if type(kind) is not SshChannelFailure:
        raise ValueError("Expected an SSH channel failure category")
    explanations = {
        SshChannelFailure.CONNECT_FAILED: (
            "SSH reported that a channel could not reach its destination. "
            "For a tunnel, check that the service is running and reachable from the final SSH server."
        ),
        SshChannelFailure.PROHIBITED: (
            "SSH reported that a channel was not permitted. "
            "For a tunnel, ask the server administrator to check forwarding permissions."
        ),
        SshChannelFailure.GENERIC: (
            "SSH reported that a channel could not be opened. "
            "For a tunnel, check the destination service and forwarding permissions."
        ),
    }
    return explanations[kind] + (
        " This does not identify a specific tunnel or necessarily end the SSH session."
    )


def local_forward_failure_guidance(
    output: bytes,
    options: SshTunnelOptions,
    exit_code: int,
    *,
    via_jump: bool = False,
) -> str | None:
    """Require the forwarding-specific fatal result; exit 255 alone proves nothing."""
    if options.is_default or exit_code != 255:
        return None
    bounded = output[-MAX_FORWARD_DIAGNOSTIC_BYTES:]
    if len(output) > len(bounded):
        # Never interpret a retained suffix as a complete first line.
        _, _, bounded = bounded.partition(b"\n")
    lines = [line.rstrip(b"\r") for line in bounded.split(b"\n")]
    if not any(_FORWARD_FATAL.fullmatch(line) for line in lines):
        return None
    route_notice = (
        " With SSH config or a jump host, another SSH process may be responsible."
        if via_jump
        else ""
    )
    ports = {rule.local_port for rule in options.local_forwards}
    for line in lines:
        match = _BIND_FAILURE.fullmatch(line)
        if match is None:
            continue
        port = int(match[1])
        if port not in ports or str(port).encode("ascii") != match[1]:
            continue
        if match[2] == b"Address already in use":
            return (
                f"SSH reported that local port 127.0.0.1:{port} is already in use. "
                "Check other sessions/apps using it. For a saved tunnel, choose another local "
                "port in session settings before reconnecting."
            ) + route_notice
        return (
            f"SSH reported that it cannot listen on local port 127.0.0.1:{port}. "
            "Check local permissions or choose an unprivileged port (1024-65535) "
            "in this session's settings before reconnecting."
        ) + route_notice
    return (
        "SSH reported that forwarding could not be set up. Check local ports and inherited "
        "SSH forwarding settings before reconnecting. The output does not identify a saved rule."
    ) + route_notice
