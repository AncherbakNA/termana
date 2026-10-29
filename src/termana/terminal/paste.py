"""Sanitize clipboard text and encode the terminal paste protocol."""

from dataclasses import dataclass
from typing import Final

BRACKETED_PASTE_START: Final = "\x1b[200~"
BRACKETED_PASTE_END: Final = "\x1b[201~"
LARGE_PASTE_CHARACTER_COUNT: Final = 4096
MAX_PASTE_CHARACTER_COUNT: Final = 128 * 1024
MAX_PASTE_PAYLOAD_BYTES: Final = 512 * 1024


class PasteTooLargeError(ValueError):
    """Raised before an unreasonably large clipboard payload is copied again."""


@dataclass(frozen=True, slots=True)
class PreparedPaste:
    """A safe terminal payload plus the facts needed for user confirmation."""

    payload: str
    plain_text: str
    has_line_breaks: bool
    is_bracketed: bool
    is_large: bool

    @property
    def requires_confirmation(self) -> bool:
        """Return whether sending could unexpectedly execute or flood input."""
        return (self.has_line_breaks and not self.is_bracketed) or self.is_large


def prepare_paste(text: str, *, bracketed: bool, one_line: bool = False) -> PreparedPaste:
    """Normalize line endings, neutralize controls, and add optional delimiters."""
    if len(text) > MAX_PASTE_CHARACTER_COUNT:
        raise PasteTooLargeError(
            "Paste cancelled: the clipboard contains "
            f"{len(text):,} characters; the limit is {MAX_PASTE_CHARACTER_COUNT:,}."
        )

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if one_line:
        normalized = normalized.replace("\n", " ")
    has_line_breaks = "\n" in normalized
    sanitized_characters: list[str] = []

    for character in normalized:
        codepoint = ord(character)
        if character in {"\t", "\n"}:
            sanitized_characters.append(character)
        elif codepoint < 32 or codepoint == 127 or 0x80 <= codepoint <= 0x9F:
            sanitized_characters.append(" ")
        else:
            sanitized_characters.append(character)

    plain_text = "".join(sanitized_characters)
    terminal_text = plain_text.replace("\n", "\r")
    payload = terminal_text
    if bracketed:
        payload = BRACKETED_PASTE_START + terminal_text + BRACKETED_PASTE_END

    return PreparedPaste(
        payload=payload,
        plain_text=plain_text,
        has_line_breaks=has_line_breaks,
        is_bracketed=bracketed,
        is_large=len(plain_text) >= LARGE_PASTE_CHARACTER_COUNT,
    )
