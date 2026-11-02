"""Parse explicit Telnet Quick Connect URIs without invoking a command shell."""

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from termana.models.connection import (
    DEFAULT_TELNET_PORT,
    MAX_NETWORK_PORT,
    MIN_NETWORK_PORT,
)

MAX_TELNET_TARGET_LENGTH: Final = 512
MAX_TELNET_HOST_LENGTH: Final = 253

_TELNET_SCHEME: Final = "telnet://"
_HOST: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_ASCII_PORT: Final = re.compile(r"[0-9]+\Z")
_DIRECTIONAL_FORMATS: Final = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)


class TelnetTargetParseError(ValueError):
    """Report a safe, user-facing error for an invalid Telnet URI."""


@dataclass(frozen=True, slots=True)
class TelnetTarget:
    """A validated endpoint entered through explicit Telnet Quick Connect."""

    host: str
    port: int = DEFAULT_TELNET_PORT


def parse_telnet_target(text: str) -> TelnetTarget:
    """Parse the deliberately small ``telnet://host[:port]`` URI subset.

    Telnet is never inferred from a bare host or from port 23. Requiring the
    scheme keeps an unencrypted connection an explicit user choice.
    """
    if not isinstance(text, str):
        raise TelnetTargetParseError("Enter a Telnet address beginning with telnet://.")

    address = text.strip(" \t")
    if not address:
        raise TelnetTargetParseError("Enter a Telnet address beginning with telnet://.")
    if len(address) > MAX_TELNET_TARGET_LENGTH:
        raise TelnetTargetParseError("The Telnet address is too long.")
    if any(_is_control_or_directional(character) for character in address):
        raise TelnetTargetParseError(
            "The Telnet address cannot contain whitespace, control, or directional characters."
        )

    authority = _remove_telnet_scheme(address)
    if any(character in authority for character in "/?#%"):
        raise TelnetTargetParseError(
            "Enter only a Telnet host and optional port, without a path, query, or options."
        )
    if "@" in authority:
        raise TelnetTargetParseError(
            "Usernames and passwords are not allowed in Telnet Quick Connect addresses."
        )

    host, port = _split_authority(authority)
    _validate_host(host)
    return TelnetTarget(host=host, port=port)


def format_telnet_target(target: TelnetTarget) -> str:
    """Return an explicit, unambiguous Telnet URI for display."""
    displayed_host = f"[{target.host}]" if ":" in target.host else target.host
    endpoint = (
        displayed_host
        if target.port == DEFAULT_TELNET_PORT and ":" not in target.host
        else f"{displayed_host}:{target.port}"
    )
    return f"{_TELNET_SCHEME}{endpoint}"


def _is_control_or_directional(character: str) -> bool:
    """Reject invisible controls, whitespace, and bidirectional formatting."""
    return (
        character.isspace()
        or unicodedata.category(character) == "Cc"
        or unicodedata.bidirectional(character) in _DIRECTIONAL_FORMATS
    )


def _remove_telnet_scheme(address: str) -> str:
    """Require the explicit Telnet scheme and return its authority."""
    if address[: len(_TELNET_SCHEME)].casefold() != _TELNET_SCHEME:
        raise TelnetTargetParseError(
            "Telnet Quick Connect requires an address beginning with telnet://."
        )
    authority = address[len(_TELNET_SCHEME) :]
    if not authority:
        raise TelnetTargetParseError("Enter a Telnet host after telnet://.")
    return authority


def _split_authority(authority: str) -> tuple[str, int]:
    """Split a hostname or a bracketed IPv6 authority from its optional port."""
    if authority.startswith("["):
        return _split_bracketed_ipv6(authority)
    if "[" in authority or "]" in authority:
        raise TelnetTargetParseError("The IPv6 address has invalid brackets.")

    colon_count = authority.count(":")
    if colon_count == 0:
        return authority, DEFAULT_TELNET_PORT
    if colon_count == 1:
        host, port_text = authority.split(":", maxsplit=1)
        if not host:
            raise TelnetTargetParseError("Enter a Telnet host before the port.")
        return host, _parse_port(port_text)

    raise TelnetTargetParseError("Use [IPv6]:port for a Telnet IPv6 address.")


def _split_bracketed_ipv6(authority: str) -> tuple[str, int]:
    """Parse the only supported IPv6 form, ``[IPv6]:port``."""
    closing_bracket = authority.find("]")
    if closing_bracket < 0:
        raise TelnetTargetParseError("Close the Telnet IPv6 address with ].")

    host = authority[1:closing_bracket]
    if not host:
        raise TelnetTargetParseError("Enter an IPv6 address inside the brackets.")
    _validate_ipv6(host)

    remainder = authority[closing_bracket + 1 :]
    if not remainder.startswith(":"):
        raise TelnetTargetParseError("Add :port after a bracketed Telnet IPv6 address.")
    return host, _parse_port(remainder[1:])


def _parse_port(port_text: str) -> int:
    """Validate a decimal network port without accepting Unicode digits."""
    if not port_text or _ASCII_PORT.fullmatch(port_text) is None:
        raise TelnetTargetParseError("The Telnet port must be a number from 1 to 65535.")
    port = int(port_text)
    if not MIN_NETWORK_PORT <= port <= MAX_NETWORK_PORT:
        raise TelnetTargetParseError("The Telnet port must be a number from 1 to 65535.")
    return port


def _validate_host(host: str) -> None:
    """Validate a conservative ASCII hostname or IP literal."""
    if not host:
        raise TelnetTargetParseError("Enter a Telnet host.")
    if ":" in host:
        _validate_ipv6(host)
        return
    if (
        len(host) > MAX_TELNET_HOST_LENGTH
        or not host.isascii()
        or _HOST.fullmatch(host) is None
        or host.startswith("-")
    ):
        raise TelnetTargetParseError(
            "The Telnet host may contain letters, numbers, dots, hyphens, and underscores."
        )


def _validate_ipv6(host: str) -> None:
    """Require a syntactically valid IPv6 address without resolving it."""
    try:
        ipaddress.IPv6Address(host)
    except ValueError as error:
        raise TelnetTargetParseError("Enter a valid bracketed Telnet IPv6 address.") from error
