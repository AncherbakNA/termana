"""Portable validation for a single explicit SSH encryption algorithm."""

import re
from typing import Final

_SSH_CIPHER_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,127}", re.ASCII)

__all__ = ("validate_ssh_cipher",)


def validate_ssh_cipher(value: object) -> str | None:
    """Preserve one literal name, leaving client support checks to OpenSSH."""
    if value is None:
        return None
    if type(value) is not str or _SSH_CIPHER_PATTERN.fullmatch(value) is None:
        raise ValueError("SSH cipher must be one literal ASCII algorithm name")
    if value.casefold() == "none":
        raise ValueError("SSH cipher cannot disable encryption")
    return value
