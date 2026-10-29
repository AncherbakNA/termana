"""Explicit, inert config projection through the ready engine's public parser."""

import shlex
import tempfile
from pathlib import Path
from uuid import uuid4

from termana.models.connection import ConnectionProfile
from termana.models.ssh_jump import SshJumpMode, SshJumpOptions, validate_ssh_route_host
from termana.models.ssh_options import SshConnectionOptions
from termana.terminal.sftp_local_files import open_sftp_source

MAX_CONFIG_BYTES = 64 * 1024
MAX_CONFIG_LINES = 2048
_DIRECTIVES = frozenset(
    {
        "host",
        "hostname",
        "port",
        "user",
        "proxyjump",
        "connecttimeout",
        "serveraliveinterval",
        "serveralivecountmax",
    }
)


class UnsupportedSshConfig(ValueError):
    def __init__(self):
        super().__init__(
            "Select a config containing only Host, HostName, Port, User, one ProxyJump, ConnectTimeout and ServerAlive options. Include, Match, commands, key/trust overrides and expansion tokens are unsupported; nothing was executed."
        )


def inert_config(data):
    if type(data) is not bytes or len(data) > MAX_CONFIG_BYTES:
        raise UnsupportedSshConfig()
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError:
        raise UnsupportedSshConfig() from None
    if len(lines) > MAX_CONFIG_LINES:
        raise UnsupportedSshConfig()
    canonical = []
    for line in lines:
        # Reject syntax which could be interpreted differently by two lexers.
        # Tokens are canonicalized before the library sees them, and the library
        # never sees unknown/exec directives even in an unmatched Host block.
        if any(char in line for char in ("\\", "\0")):
            raise UnsupportedSshConfig()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped.replace("=", " ", 1), comments=True)
        except ValueError:
            raise UnsupportedSshConfig() from None
        if len(tokens) < 2 or tokens[0].lower() not in _DIRECTIVES:
            raise UnsupportedSshConfig()
        keyword, values = tokens[0].lower(), tokens[1:]
        if keyword != "host" and len(values) != 1:
            raise UnsupportedSshConfig()
        if any(any(char in value for char in ("%", "$", "`", "=", "\r", "\n")) for value in values):
            raise UnsupportedSshConfig()
        if keyword == "proxyjump" and "," in values[0]:
            raise UnsupportedSshConfig()
        canonical.append(keyword + " " + " ".join(shlex.quote(value) for value in values))
    return "\n".join(canonical).encode("utf-8") + b"\n"


def _parse_gateway(value):
    # Exactly one explicit user@host[:port]; IPv6 uses brackets here.
    if type(value) is not str or value.count("@") != 1:
        raise UnsupportedSshConfig()
    user, host = value.split("@")
    port = None
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            raise UnsupportedSshConfig()
        tail = host[end + 1 :]
        if tail:
            if not tail.startswith(":"):
                raise UnsupportedSshConfig()
            port = int(tail[1:])
        host = host[1:end]
    elif host.count(":") == 1:
        host, raw_port = host.split(":")
        port = int(raw_port)
    elif ":" in host:
        raise UnsupportedSshConfig()  # ProxyJump IPv6 must be unambiguous/bracketed.
    validate_ssh_route_host(host)
    # Validate before constructing parser overrides. In particular port=0 must
    # never disappear into a truthiness check and silently adopt config/defaults.
    SshJumpOptions(SshJumpMode.JUMP, host, port, user)
    return user, host, port


def resolve_config_snapshot(path: Path, alias: str) -> ConnectionProfile:
    """No network, key/vault/trust read or command execution; no persistent store write."""
    validate_ssh_route_host(alias)
    with open_sftp_source(path) as stream:
        sanitized = inert_config(stream.read(MAX_CONFIG_BYTES + 1))
    # Appending a last-match fallback uses the ready parser's first-value
    # semantics. It distinguishes a selected config User from the library's
    # implicit local username, without reading its private parser structures.
    unspecified_user = "termana_unset_" + uuid4().hex
    sanitized += f"Host *\n User {unspecified_user}\n".encode("ascii")
    import asyncssh

    # Feed only this immutable sanitized snapshot, not a re-open of user data.
    # The private temporary file is not attached to any resulting connection.
    with tempfile.TemporaryDirectory(prefix="termana-config-") as directory:
        temporary = Path(directory) / "config"
        with temporary.open("xb") as stream:
            stream.write(sanitized)

        def options(host, **overrides):
            return asyncssh.SSHClientConnectionOptions(
                config=[temporary],
                host=host,
                client_keys=None,
                client_certs=[],
                known_hosts=[],
                x509_trusted_certs=[],
                x509_trusted_cert_paths=[],
                agent_path=None,
                gss_host=None,
                **overrides,
            )

        effective = options(alias)
        validate_ssh_route_host(effective.host)
        route = SshJumpOptions(SshJumpMode.DIRECT)
        if effective.tunnel:
            user, host, port = _parse_gateway(effective.tunnel)
            gateway = options(
                host, username=user, tunnel=None, **({"port": port} if port is not None else {})
            )
            # A gateway which itself needs another gateway is unsupported.
            # Resolve with config first, then require the explicit single-hop boundary.
            if options(host).tunnel:
                raise UnsupportedSshConfig()
            route = SshJumpOptions(SshJumpMode.JUMP, gateway.host, gateway.port, gateway.username)

        def integer(value):
            parsed = int(value)
            if parsed != value:
                raise UnsupportedSshConfig()
            return parsed

        interval = integer(effective.keepalive_interval)
        timers = SshConnectionOptions(
            connect_timeout=None
            if effective.connect_timeout is None
            else integer(effective.connect_timeout),
            server_alive_interval=interval,
            server_alive_count_max=integer(effective.keepalive_count_max) if interval > 0 else None,
        )
        return ConnectionProfile(
            "Config · " + alias,
            effective.host,
            port=effective.port,
            username=None if effective.username == unspecified_user else effective.username,
            ssh_jump=route,
            ssh_options=timers,
        )
