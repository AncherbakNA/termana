"""Explicit plain-text copy with honest immediate OS-clipboard feedback."""

from PySide6.QtGui import QGuiApplication


def copy_plain_text(text):
    clipboard = QGuiApplication.clipboard()
    clipboard.setText(text)
    # Qt setText has no success return. An inaccessible/busy clipboard can reject
    # it; never promise success. This is an immediate check, not future ownership.
    return clipboard.text() == text
