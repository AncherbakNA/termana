"""Parse user-entered SSH destinations without invoking a command shell."""

import ipaddress
import re
from dataclasses import dataclass
from typing import Final

from termana.models.connection import DEFAULT_SSH_PORT, MAX_NETWORK_PORT, MIN_NETWORK_PORT

MAX_SSH_TARGET_LENGTH: Final = 512
MAX_SSH_HOST_LENGTH: Final = 253
MAX_SSH_USERNAME_LENGTH: Final = 128

_USERNAME: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_HOST: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_ASCII_PORT: Final = re.compile(r"[0-9]+\Z")
_DISALLOWED_ADDRESS_CHARACTERS: Final = frozenset("/?#%")


class SshTargetParseError(ValueError):
    """Report a safe, user-facing error for an invalid SSH destination."""


@dataclass(frozen=True, slots=True)
class SshTarget:
    """A validated SSH endpoint entered through Quick Connect."""

    host: str
    port: int = DEFAULT_SSH_PORT
    username: str | None = None


def parse_ssh_target(text: str) -> SshTarget:
    """Parse a small, explicit subset of SSH destination syntax."""
    if not isinstance(text, str):
        raise SshTargetParseError("Enter an SSH host, for example user@example.com.")

    address = text.strip(" \t")
    if not address:
        raise SshTargetParseError("Enter an SSH host, for example user@example.com.")
    if len(address) > MAX_SSH_TARGET_LENGTH:
        raise SshTargetParseError("The SSH address is too long.")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in address
    ):
        raise SshTargetParseError(
            "The SSH address cannot contain whitespace or control characters."
        )

    address = _remove_ssh_scheme(address)
    if any(character in address for character in _DISALLOWED_ADDRESS_CHARACTERS):
        raise SshTargetParseError("Enter only an SSH host, without a path, query, or options.")

    username, destination = _split_username(address)
    host, port = _split_destination(destination)
    _validate_username(username)
    _validate_host(host)
    return SshTarget(host=host, port=port, username=username)


def format_ssh_target(target: SshTarget) -> str:
    """Return a concise, unambiguous tab title for an SSH target."""
    displayed_host = f"[{target.host}]" if ":" in target.host else target.host
    endpoint = (
        displayed_host if target.port == DEFAULT_SSH_PORT else f"{displayed_host}:{target.port}"
    )
    return f"{target.username}@{endpoint}" if target.username else endpoint


def _remove_ssh_scheme(address: str) -> str:
    """Accept only the explicit ssh:// scheme and return its authority."""
    scheme, separator, remainder = address.partition("://")
    if not separator:
        return address
    if scheme.casefold() != "ssh":
        raise SshTargetParseError("Quick Connect supports only SSH addresses.")
    if not remainder:
        raise SshTargetParseError("Enter an SSH host after ssh://.")
    return remainder


def _split_username(address: str) -> tuple[str | None, str]:
    """Split optional user information while explicitly refusing passwords."""
    if address.count("@") > 1:
        raise SshTargetParseError("Use at most one @ between the username and host.")
    if "@" not in address:
        return None, address

    username, destination = address.split("@", maxsplit=1)
    if not username:
        raise SshTargetParseError("Enter a username before @.")
    if ":" in username:
        raise SshTargetParseError("Passwords are not allowed in Quick Connect.")
    if not destination:
        raise SshTargetParseError("Enter a host after @.")
    return username, destination


def _split_destination(destination: str) -> tuple[str, int]:
    """Split host and port, preserving an unbracketed IPv6 address as one host."""
    if destination.startswith("["):
        return _split_bracketed_ipv6(destination)
    if "[" in destination or "]" in destination:
        raise SshTargetParseError("The IPv6 address has invalid brackets.")

    colon_count = destination.count(":")
    if colon_count == 0:
        return destination, DEFAULT_SSH_PORT
    if colon_count == 1:
        host, port_text = destination.split(":", maxsplit=1)
        if not host:
            raise SshTargetParseError("Enter a host before the port.")
        return host, _parse_port(port_text)

    _validate_ipv6(destination)
    return destination, DEFAULT_SSH_PORT


def _split_bracketed_ipv6(destination: str) -> tuple[str, int]:
    """Parse [IPv6] and [IPv6]:port forms."""
    closing_bracket = destination.find("]")
    if closing_bracket < 0:
        raise SshTargetParseError("Close the IPv6 address with ].")

    host = destination[1:closing_bracket]
    if not host:
        raise SshTargetParseError("Enter an IPv6 address inside the brackets.")
    _validate_ipv6(host)

    remainder = destination[closing_bracket + 1 :]
    if not remainder:
        return host, DEFAULT_SSH_PORT
    if not remainder.startswith(":"):
        raise SshTargetParseError("Only :port may follow a bracketed IPv6 address.")
    return host, _parse_port(remainder[1:])


def _parse_port(port_text: str) -> int:
    """Validate a decimal network port without accepting Unicode digits."""
    if not port_text or _ASCII_PORT.fullmatch(port_text) is None:
        raise SshTargetParseError("The SSH port must be a number from 1 to 65535.")
    port = int(port_text)
    if not MIN_NETWORK_PORT <= port <= MAX_NETWORK_PORT:
        raise SshTargetParseError("The SSH port must be a number from 1 to 65535.")
    return port


def _validate_username(username: str | None) -> None:
    """Accept the conservative username subset supported by Quick Connect v1."""
    if username is None:
        return
    if (
        len(username) > MAX_SSH_USERNAME_LENGTH
        or not username.isascii()
        or _USERNAME.fullmatch(username) is None
    ):
        raise SshTargetParseError(
            "The username may contain letters, numbers, dots, hyphens, and underscores."
        )


def _validate_host(host: str) -> None:
    """Validate an IPv6 address or a conservative DNS/SSH alias."""
    if not host:
        raise SshTargetParseError("Enter an SSH host.")
    if ":" in host:
        _validate_ipv6(host)
        return
    if (
        len(host) > MAX_SSH_HOST_LENGTH
        or not host.isascii()
        or _HOST.fullmatch(host) is None
        or host.startswith("-")
    ):
        raise SshTargetParseError(
            "The host may contain letters, numbers, dots, hyphens, and underscores."
        )


def _validate_ipv6(host: str) -> None:
    """Require a syntactically valid IPv6 address without resolving it."""
    try:
        ipaddress.IPv6Address(host)
    except ValueError as error:
        raise SshTargetParseError(
            "Enter a valid IPv6 address; use [address]:port for a custom port."
        ) from error
