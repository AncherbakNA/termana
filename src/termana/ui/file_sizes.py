"""Consistent IEC sizes in the application language, not the OS locale."""

from termana.models.settings import InterfaceLanguage
from termana.ui.i18n import current_language, tr


def file_size_text(size: int | None) -> str:
    """Format presentation only; sorting and file operations keep exact bytes."""
    if type(size) is not int or size < 0:
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    value = size
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    number = str(size) if not index else f"{value:.1f}".removesuffix(".0")
    if current_language() is InterfaceLanguage.RUSSIAN:
        number = number.replace(".", ",")
    return number + " " + tr(units[index], context="file-size")


def exact_file_size_text(size: int) -> str:
    return tr("{p0} bytes", p0=str(size))
