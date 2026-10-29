"""Validate explicit, session-local tab labels before they reach the UI."""

import unicodedata
from typing import Final

MAX_CUSTOM_TAB_TITLE_CHARACTERS: Final = 80


def validate_custom_tab_title(value: str) -> str:
    """Return a normalized label, rejecting unsafe or out-of-bounds input."""
    if not isinstance(value, str):
        raise TypeError("Tab title must be a string")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    ):
        raise ValueError("Tab title cannot contain control or non-printing characters")

    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise ValueError("Tab title cannot be empty")
    if len(normalized) > MAX_CUSTOM_TAB_TITLE_CHARACTERS:
        raise ValueError(f"Tab title must be {MAX_CUSTOM_TAB_TITLE_CHARACTERS} characters or fewer")
    return normalized
