"""Resolve portable terminal-font preferences through the local Qt database."""

from PySide6.QtGui import QFont, QFontDatabase

from termana.models.settings import TerminalFontSettings


def available_terminal_font_families() -> tuple[str, ...]:
    """Return installed fixed-pitch families in stable display order."""
    return tuple(
        sorted(
            {
                family
                for family in QFontDatabase.families()
                if QFontDatabase.isFixedPitch(family) and not QFontDatabase.isPrivateFamily(family)
            },
            key=str.casefold,
        )
    )


def resolve_terminal_font(settings: TerminalFontSettings) -> QFont:
    """Build a safe local font from portable settings and system fallbacks."""
    if not isinstance(settings, TerminalFontSettings):
        raise TypeError("settings must be TerminalFontSettings")

    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFixedPitch(True)
    if (
        settings.family is not None
        and QFontDatabase.hasFamily(settings.family)
        and QFontDatabase.isFixedPitch(settings.family)
        and not QFontDatabase.isPrivateFamily(settings.family)
    ):
        font.setFamily(settings.family)
    font.setPointSize(settings.point_size)
    font.setKerning(False)
    return font
