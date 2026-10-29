"""Immutable, bounded, non-secret SSH negotiation facts for one destination."""

import re
from dataclasses import dataclass, fields
from typing import Final

MAX_SSH_ALGORITHM_CHARACTERS: Final = 128
_ALGORITHM: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+/-]{0,127}", re.ASCII)
_SHA256_FINGERPRINT: Final = re.compile(r"SHA256:[A-Za-z0-9+/]{43}", re.ASCII)


def bounded_ssh_algorithm(value: object) -> str | None:
    """Unknown, malformed or display-control values remain explicitly unavailable."""
    return value if type(value) is str and _ALGORITHM.fullmatch(value) else None


def bounded_ssh_fingerprint(value: object) -> str | None:
    return value if type(value) is str and _SHA256_FINGERPRINT.fullmatch(value) else None


@dataclass(frozen=True, slots=True)
class SshNegotiatedFacts:
    """Auth-complete snapshot, not credentials, gateway facts or live rekey status."""

    send_cipher: str | None = None
    recv_cipher: str | None = None
    send_mac: str | None = None
    recv_mac: str | None = None
    send_compression: str | None = None
    recv_compression: str | None = None
    server_key_algorithm: str | None = None
    server_key_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            validate = (
                bounded_ssh_fingerprint
                if field.name == "server_key_fingerprint"
                else bounded_ssh_algorithm
            )
            if value is not None and validate(value) is None:
                raise ValueError("Invalid bounded SSH negotiation fact")
