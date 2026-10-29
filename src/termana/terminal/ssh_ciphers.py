"""Discover locally supported ciphers from the trusted system OpenSSH client."""

import subprocess
import sys
from typing import Final

from termana.models.ssh_cipher import validate_ssh_cipher
from termana.terminal.ssh_command import resolve_system_ssh_program

SSH_CIPHER_QUERY_TIMEOUT_SECONDS: Final = 2.0
MAX_SSH_CIPHER_QUERY_BYTES: Final = 64 * 1024
MAX_DISCOVERED_SSH_CIPHERS: Final = 128
_MODERN_SSH_CIPHERS: Final = frozenset(
    {
        "aes128-ctr",
        "aes192-ctr",
        "aes256-ctr",
        "aes128-gcm@openssh.com",
        "aes256-gcm@openssh.com",
        "chacha20-poly1305@openssh.com",
    }
)


class SshCipherDiscoveryError(RuntimeError):
    """A safe, non-sensitive explanation for an unavailable cipher list."""


def discover_ssh_ciphers() -> tuple[str, ...]:
    """Query local support only; callers must run this off the UI thread.

    ``ssh -Q cipher`` neither connects to a host nor reveals host configuration.
    The result is support information, not the default or a recommended order.
    """
    program = resolve_system_ssh_program()
    if program is None:
        raise SshCipherDiscoveryError("A trusted system SSH client is unavailable.")
    try:
        completed = subprocess.run(
            [program, "-Q", "cipher"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=SSH_CIPHER_QUERY_TIMEOUT_SECONDS,
            check=False,
            # A local capability query is not a terminal session. GUI Python on
            # Windows must not allocate a transient ssh.exe console for it.
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
        )
    except subprocess.TimeoutExpired:
        raise SshCipherDiscoveryError("The system SSH cipher query timed out.") from None
    except OSError:
        raise SshCipherDiscoveryError("The system SSH cipher query could not run.") from None
    if completed.returncode != 0:
        raise SshCipherDiscoveryError("The system SSH client could not list its ciphers.")
    output = completed.stdout
    if not isinstance(output, bytes) or len(output) > MAX_SSH_CIPHER_QUERY_BYTES:
        raise SshCipherDiscoveryError("The system SSH client returned an invalid cipher list.")
    try:
        lines = output.decode("ascii").split("\n")
    except UnicodeError:
        raise SshCipherDiscoveryError(
            "The system SSH client returned an invalid cipher list."
        ) from None
    if lines[-1] == "":
        lines.pop()
    names = [line.removesuffix("\r") for line in lines]
    if not names or len(names) > MAX_DISCOVERED_SSH_CIPHERS:
        raise SshCipherDiscoveryError("The system SSH client returned an invalid cipher list.")
    unique_names: dict[str, None] = {}
    try:
        for name in names:
            validate_ssh_cipher(name)
            unique_names[name] = None
    except ValueError:
        raise SshCipherDiscoveryError(
            "The system SSH client returned an invalid cipher list."
        ) from None
    return tuple(unique_names)


def is_legacy_ssh_cipher(name: str) -> bool:
    """Conservatively label anything outside the known modern cipher set.

    This compatibility label is not a security guarantee or a support check.
    Future algorithms remain in the compatibility group until reviewed.
    """
    return name not in _MODERN_SSH_CIPHERS
