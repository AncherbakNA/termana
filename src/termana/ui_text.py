"""Qt-specific presentation helpers for untrusted user-visible text."""

from html import escape


def plain_text_tooltip(value: str) -> str:
    """Make Qt render an exact tooltip value as inert text, never as rich text."""
    if not isinstance(value, str):
        raise TypeError("Tooltip value must be text")
    return f"<qt>{escape(value, quote=True)}</qt>"


def escape_qt_mnemonic(value: str) -> str:
    """Preserve literal ampersands in labels backed by untrusted metadata."""
    if not isinstance(value, str):
        raise TypeError("Qt label value must be text")
    return value.replace("&", "&&")
