"""Original stylesheet fragments grouped by UI responsibility."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.ui.theme import ThemeColors


def widgets_styles(colors: ThemeColors) -> str:
    """Original widgets rules; whitespace and cascade are deliberate."""
    return f"""
        QWidget {{
            background-color: {colors.background};
            color: {colors.text};
            selection-background-color: {colors.accent};
            selection-color: {colors.accent_text};
        }}

        QWidget:disabled {{
            color: {colors.muted_text};
        }}

        ThemedCheckBox {{
            spacing: 7px;
        }}

        ThemedCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border: 1px solid {colors.control_border};
            border-radius: 3px;
            background-color: {colors.surface};
            image: none;
        }}

        ThemedCheckBox::indicator:checked,
        ThemedCheckBox::indicator:indeterminate {{
            border-color: {colors.accent};
            background-color: {colors.raised};
        }}

        ThemedCheckBox::indicator:hover,
        ThemedCheckBox::indicator:focus {{
            border-color: {colors.accent};
        }}

        ThemedCheckBox::indicator:disabled {{
            border-color: {colors.border};
            background-color: {colors.raised};
        }}

        QLabel {{
            background-color: transparent;
        }}

        QLabel#sftpOwnerLabel {{
            font-weight: 600;
            color: {colors.text};
        }}

        QLabel#sftpConnectionNote,
        QLabel#sftpConnectionBadge,
        QLabel#sftpListingSummary,
        QLabel#sftpSelectionSummary {{
            color: {colors.muted_text};
        }}
"""
