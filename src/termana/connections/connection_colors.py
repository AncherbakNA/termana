"""User-selected connection accents with theme-safe non-text icon contrast."""

from PySide6.QtGui import QColor

CONNECTION_COLORS = (
    ("Blue", "#4778DD"), ("Cyan", "#168FA6"), ("Green", "#27894D"),
    ("Amber", "#B58319"), ("Orange", "#CB6929"), ("Red", "#CF4855"),
    ("Purple", "#8F5ACB"), ("Pink", "#C74C98"), ("Gray", "#747D8B"),
)


def _luminance(color: QColor) -> float:
    def linear(value):
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    return sum(weight * linear(value) for weight, value in
               zip((0.2126, 0.7152, 0.0722), color.getRgbF()[:3], strict=True))


def contrast_ratio(foreground: str, background: str) -> float:
    levels = sorted((_luminance(QColor(foreground)), _luminance(QColor(background))))
    return (levels[1] + 0.05) / (levels[0] + 0.05)


def connection_icon_color(accent: str, background: str, *other_backgrounds: str) -> str:
    """Keep a chosen hue visible, adapting only its displayed icon tone."""
    backgrounds = (background, *other_backgrounds)
    def visible(tone):
        return all(contrast_ratio(tone, surface) >= 3 for surface in backgrounds)

    if visible(accent):
        return accent
    original = QColor(accent)
    destination = QColor("#FFFFFF" if _luminance(QColor(background)) < 0.179 else "#000000")
    for step in range(1, 21):
        weight = step / 20
        color = QColor.fromRgbF(*(start + (end - start) * weight for start, end in
            zip(original.getRgbF()[:3], destination.getRgbF()[:3], strict=True)))
        if visible(color.name()):
            return color.name().upper()
    return destination.name().upper()
