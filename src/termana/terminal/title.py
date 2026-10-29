"""Normalize untrusted terminal titles before they reach Qt widgets."""

import unicodedata
from typing import Final

MAX_TERMINAL_TITLE_CHARACTERS: Final = 128
_UNSAFE_UNICODE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs"})


def sanitize_terminal_title(title: str) -> str:
    """Return a compact printable title safe for a single-line tab label."""
    printable = "".join(
        " "
        if character.isspace() or unicodedata.category(character) in _UNSAFE_UNICODE_CATEGORIES
        else character
        for character in title
    )
    normalized = unicodedata.normalize("NFC", printable)
    compact = " ".join(normalized.split())
    if len(compact) <= MAX_TERMINAL_TITLE_CHARACTERS:
        return compact

    prefix = compact[: MAX_TERMINAL_TITLE_CHARACTERS - 1].rstrip()
    return f"{prefix}…"
