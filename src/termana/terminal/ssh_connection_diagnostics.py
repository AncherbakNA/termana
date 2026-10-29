"""Bounded advisory hints, never proof of a remote host's state or identity."""

import re

MAX_CONNECTION_DIAGNOSTIC_BYTES = 64 * 1024
_KEEPALIVE_TIMEOUT = re.compile(
    rb"(?:clientloop\.c:server_alive_check\(\):[0-9]{1,10} "
    rb"\((?:bin=[\x20-\x7e]{1,1024}, )?pid=[0-9]{1,10}\): )?"
    rb"Timeout, server [\x21-\x7e]{1,255} not responding\."
)

# These messages are advisory only. Feed this classifier the private OpenSSH
# diagnostic stream, never a PTY transcript (which includes remote output).
_HOST = rb"[\x21-\x7e]{1,255}"
_CONNECT_FAILURE = re.compile(
    rb"ssh: connect to host " + _HOST + rb" port [0-9]{1,5}: "
    rb"(Connection refused|Connection timed out|Operation timed out|"
    rb"No route to host|Network is unreachable)"
)
_RESOLUTION_FAILURE = re.compile(
    rb"ssh: Could not resolve hostname " + _HOST + rb": "
    rb"(?:Name or service not known|Temporary failure in name resolution|"
    rb"nodename nor servname provided, or not known|No address associated with hostname)"
)
_AUTHENTICATION_FAILURE = re.compile(
    rb"[\x21-\x7e]{1,255}@" + _HOST + rb": Permission denied \([a-z0-9@.,_-]{1,512}\)\."
)
_ALGORITHM_FAILURE = re.compile(
    rb"Unable to negotiate with " + _HOST + rb" port [0-9]{1,5}: no matching "
    rb"(cipher|MAC|key exchange method|host key type) found\. Their offer: "
    rb"[A-Za-z0-9@._+,/=-]{1,2048}"
)
_AUTHENTICATION_LIMIT = re.compile(
    rb"Received disconnect from " + _HOST + rb" port [0-9]{1,5}:2: "
    rb"Too many authentication failures(?: for [\x21-\x7e]{1,255})?"
)
_RESET_FAILURE = re.compile(rb"Connection reset by " + _HOST + rb" port [0-9]{1,5}")
_BROKEN_PIPE = re.compile(rb"client_loop: send disconnect: Broken pipe")
_PACKET_DIAGNOSTIC = re.compile(
    rb"packet\.c:sshpkt_vfatal\(\):[0-9]{1,10} "
    rb"\((?:bin=[\x20-\x7e]{1,1024}, )?pid=[0-9]{1,10}\): (.+)"
)
_ALGORITHM_SETTINGS = {
    b"cipher": "Encryption in the session settings and Ciphers in SSH config",
    b"MAC": "MACs in SSH config",
    b"key exchange method": "KexAlgorithms in SSH config",
    b"host key type": "HostKeyAlgorithms in SSH config",
}
_FAILURE_GUIDANCE = {
    b"Connection refused": (
        "SSH could not connect because the connection was refused. Check the server address "
        "and SSH port, and whether the SSH service is running and the port is allowed."
    ),
    b"timeout": (
        "SSH could not establish the connection before it timed out. Check your network, VPN, "
        "server availability, and firewall. If the connection is expected to be slow, review "
        "the initial connection timeout in the session settings."
    ),
    b"route": (
        "SSH reported that the network or host is unreachable. Check your network, VPN, "
        "and the route to the server."
    ),
    b"address": (
        "SSH could not resolve the server name. Check the spelling of the address, "
        "your DNS settings, and whether a VPN is required."
    ),
    b"authentication": (
        "SSH authentication was not accepted. Check the username and the server's allowed "
        "sign-in methods. Depending on those methods, check your password, SSH key, or agent. "
        "This does not necessarily mean the password is incorrect."
    ),
    b"authentication-limit": (
        "SSH reported too many authentication attempts. This can happen when an agent offers "
        "several keys before the intended one. Review SSH keys in the session settings: "
        "an explicit identity with IdentitiesOnly can limit configured key offers. "
        "Check the server's sign-in policy before retrying; no password or agent key was removed."
    ),
    b"reset": (
        "SSH reported a connection reset. The server or a network device may have ended "
        "the connection. Check your network, VPN, and server logs, then reconnect when ready. "
        "This message does not establish whether sign-in completed."
    ),
    b"broken-pipe": (
        "SSH could no longer write to the connection (broken pipe). Check network/VPN "
        "availability and server idle-timeout policy. Keepalive settings may help detect "
        "silent failures sooner, but do not guarantee that a connection stays open. "
        "Reconnect manually when ready."
    ),
}


def ssh_connection_failure_guidance(
    diagnostics: bytes, exit_code: int, *, via_jump: bool = False
) -> str | None:
    """Explain a complete local diagnostic without reflecting addresses or log text.

    The caller must supply only the private OpenSSH log. At its retention limit,
    discard the first line: its beginning may already have been truncated upstream.
    An incomplete trailing line cannot establish a recognized failure either.
    """
    if exit_code != 255:
        return None
    bounded = diagnostics[-MAX_CONNECTION_DIAGNOSTIC_BYTES:]
    if len(diagnostics) >= MAX_CONNECTION_DIAGNOSTIC_BYTES:
        _, _, bounded = bounded.partition(b"\n")
    for line in reversed(bounded.split(b"\n")[:-1]):
        line = line.rstrip(b"\r")
        packet_diagnostic = _PACKET_DIAGNOSTIC.fullmatch(line)
        if packet_diagnostic is not None:
            line = packet_diagnostic[1]
            # Only this narrowly enabled local fatal function may prefix these
            # messages. A random debug/server message cannot supply a prefix.
            if not (_ALGORITHM_FAILURE.fullmatch(line) or _RESET_FAILURE.fullmatch(line)):
                continue
        algorithm = _ALGORITHM_FAILURE.fullmatch(line)
        failure = _CONNECT_FAILURE.fullmatch(line)
        if algorithm is not None:
            message = (
                "SSH could not agree on a compatible " + algorithm[1].decode("ascii") + ". "
                "Review " + _ALGORITHM_SETTINGS[algorithm[1]] + " with the server administrator. "
                "Prefer updating the server's supported algorithms; do not enable legacy "
                "algorithms or disable host verification just to bypass this error."
            )
            return _with_route_context(message, via_jump)
        if failure is not None:
            reason = failure[1]
            if reason in (b"Connection timed out", b"Operation timed out"):
                reason = b"timeout"
            elif reason in (b"No route to host", b"Network is unreachable"):
                reason = b"route"
        elif _RESOLUTION_FAILURE.fullmatch(line):
            reason = b"address"
        elif _AUTHENTICATION_FAILURE.fullmatch(line):
            reason = b"authentication"
        elif _AUTHENTICATION_LIMIT.fullmatch(line):
            reason = b"authentication-limit"
        elif _RESET_FAILURE.fullmatch(line):
            reason = b"reset"
        elif _BROKEN_PIPE.fullmatch(line):
            reason = b"broken-pipe"
        else:
            continue
        return _with_route_context(_FAILURE_GUIDANCE[reason], via_jump)
    return None


def _with_route_context(message: str, via_jump: bool) -> str:
    if via_jump:
        message += " A jump host may be the connection that failed; check that route too."
    else:
        message += " Inherited SSH config may also affect the connection route."
    return message + " No automatic retry or settings changes were made."


def ssh_keepalive_timeout_guidance(
    output: bytes, exit_code: int, *, via_jump: bool = False
) -> str | None:
    """Recognize a complete OpenSSH timeout line without echoing its hostname."""
    if exit_code != 255:
        return None
    bounded = output[-MAX_CONNECTION_DIAGNOSTIC_BYTES:]
    if len(output) > len(bounded):
        _, _, bounded = bounded.partition(b"\n")
    if not any(_KEEPALIVE_TIMEOUT.fullmatch(line.rstrip(b"\r")) for line in bounded.split(b"\n")):
        return None
    message = (
        "SSH reported no response to its keepalive checks. Check your network, VPN, "
        "and server availability, then reconnect when ready. "
        "No automatic reconnect or settings changes were made."
    )
    if via_jump:
        message += " SSH config or a jump host may affect which connection stopped responding."
    return message
