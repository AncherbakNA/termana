"""Color themes and Qt style sheet generation for Termana."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final

from termana.models.settings import ThemeId
from termana.ui.styles.base import (
    widgets_styles,
)
from termana.ui.styles.chrome import (
    menus_styles,
    sidebar_buttons_styles,
    status_styles,
    toolbar_styles,
)
from termana.ui.styles.dialogs import (
    host_keys_styles,
    messages_styles,
    saved_logins_styles,
    ssh_controls_styles,
)
from termana.ui.styles.forms import (
    danger_buttons_styles,
    dialog_surfaces_styles,
    fields_styles,
    primary_buttons_styles,
    progress_styles,
    scrollbars_styles,
    secondary_buttons_styles,
    tables_styles,
)
from termana.ui.styles.sessions import (
    filter_styles,
    tree_styles,
)
from termana.ui.styles.terminal import (
    search_styles,
    start_styles,
    surface_styles,
    tabs_styles,
)


@dataclass(frozen=True, slots=True)
class AnsiColors:
    """The sixteen standard colors that terminal applications can request."""

    black: str
    red: str
    green: str
    yellow: str
    blue: str
    magenta: str
    cyan: str
    white: str
    bright_black: str
    bright_red: str
    bright_green: str
    bright_yellow: str
    bright_blue: str
    bright_magenta: str
    bright_cyan: str
    bright_white: str


@dataclass(frozen=True, slots=True)
class TerminalColors:
    """Colors owned by the terminal renderer rather than the general UI."""

    foreground: str
    background: str
    cursor: str
    cursor_text: str
    selection_background: str
    selection_foreground: str
    ansi: AnsiColors


@dataclass(frozen=True, slots=True)
class ThemeColors:
    """Named color roles used throughout the application."""

    background: str
    surface: str
    raised: str
    text: str
    muted_text: str
    border: str
    control_border: str
    accent: str
    accent_text: str
    warning: str
    warning_surface: str
    warning_text: str
    danger: str
    danger_text: str
    terminal: TerminalColors


# Keep the original dark/light storage IDs compatible: both now use Graphite.
DARK_THEME = ThemeColors(
    background="#202226",
    surface="#202226",
    raised="#2B2E34",
    text="#E4E7ED",
    muted_text="#A5ADBA",
    border="#343840",
    control_border="#798496",
    accent="#8DA5EB",
    accent_text="#151D32",
    warning="#F2CC60",
    warning_surface="#302918",
    warning_text="#FFE39A",
    danger="#F26B6B",
    danger_text="#190707",
    terminal=TerminalColors(
        foreground="#E4E7ED",
        background="#16181B",
        cursor="#8DA5EB",
        cursor_text="#151D32",
        selection_background="#344466",
        selection_foreground="#E4E7ED",
        ansi=AnsiColors(
            black="#0E141B",
            red="#F26B6B",
            green="#7BD88F",
            yellow="#F2CC60",
            blue="#6CB6FF",
            magenta="#C099FF",
            cyan="#38D6C4",
            white="#E8EEF4",
            bright_black="#667788",
            bright_red="#FF8A8A",
            bright_green="#A3F7B5",
            bright_yellow="#FFE083",
            bright_blue="#8CC8FF",
            bright_magenta="#D6B8FF",
            bright_cyan="#72EFE1",
            bright_white="#FFFFFF",
        ),
    ),
)

LIGHT_THEME = ThemeColors(
    background="#F5F6F8",
    surface="#FFFFFF",
    raised="#ECEFF3",
    text="#252A33",
    muted_text="#636D7C",
    border="#DCE1E8",
    control_border="#798496",
    accent="#4566C9",
    accent_text="#FFFFFF",
    warning="#B7791F",
    warning_surface="#FFF4CE",
    warning_text="#6B4500",
    danger="#C43D3D",
    danger_text="#FFFFFF",
    terminal=TerminalColors(
        foreground="#252A33",
        background="#FFFFFF",
        cursor="#4566C9",
        cursor_text="#FFFFFF",
        selection_background="#E3E9F6",
        selection_foreground="#252A33",
        ansi=AnsiColors(
            black="#16212B",
            red="#B42318",
            green="#18794E",
            yellow="#946200",
            blue="#175CD3",
            magenta="#7F56D9",
            cyan="#0E7490",
            white="#475467",
            bright_black="#667085",
            bright_red="#C4320A",
            bright_green="#087A48",
            bright_yellow="#825500",
            bright_blue="#155EEF",
            bright_magenta="#7047C8",
            bright_cyan="#0B7189",
            bright_white="#101828",
        ),
    ),
)

WARM_THEME = replace(
    DARK_THEME,
    background="#25231F",
    surface="#292722",
    raised="#35312A",
    text="#E9E6DF",
    muted_text="#B0A899",
    border="#423D34",
    control_border="#948773",
    accent="#CBAE84",
    accent_text="#251E14",
    terminal=replace(
        DARK_THEME.terminal,
        foreground="#E9E6DF",
        background="#1C1B18",
        cursor="#CBAE84",
        cursor_text="#251E14",
        selection_background="#4B3F2D",
        selection_foreground="#E9E6DF",
    ),
)
WARM_LIGHT_THEME = replace(
    LIGHT_THEME,
    background="#F6F4EF",
    surface="#FAF9F6",
    raised="#E9E5DC",
    text="#302C26",
    muted_text="#6C6458",
    border="#DCD6CA",
    control_border="#8A7D68",
    accent="#816037",
    terminal=replace(
        LIGHT_THEME.terminal,
        foreground="#302C26",
        background="#FFFEFA",
        cursor="#816037",
        selection_background="#E9DFCB",
        selection_foreground="#302C26",
    ),
)
MONOCHROME_THEME = replace(
    DARK_THEME,
    background="#202022",
    surface="#242426",
    raised="#303033",
    text="#E9E9ED",
    muted_text="#ADADB7",
    border="#3B3B40",
    control_border="#868690",
    accent="#DDDDE5",
    accent_text="#202024",
    terminal=replace(
        DARK_THEME.terminal,
        foreground="#E9E9ED",
        background="#171719",
        cursor="#DDDDE5",
        cursor_text="#202024",
        selection_background="#414147",
        selection_foreground="#E9E9ED",
    ),
)
MONOCHROME_LIGHT_THEME = replace(
    LIGHT_THEME,
    background="#F5F5F6",
    raised="#EAEAED",
    text="#28282D",
    muted_text="#676771",
    border="#DEDEE3",
    control_border="#858591",
    accent="#414148",
    terminal=replace(
        LIGHT_THEME.terminal,
        foreground="#28282D",
        cursor="#414148",
        selection_background="#E4E4E9",
        selection_foreground="#28282D",
    ),
)

THEME_LABELS: Final[Mapping[ThemeId, str]] = MappingProxyType(
    {
        ThemeId.DARK: "Graphite · Dark (default)",
        ThemeId.LIGHT: "Graphite · Light",
        ThemeId.WARM: "Warm · Dark",
        ThemeId.WARM_LIGHT: "Warm · Light",
        ThemeId.MONOCHROME: "Monochrome · Dark",
        ThemeId.MONOCHROME_LIGHT: "Monochrome · Light",
    }
)

THEMES: Final[Mapping[ThemeId, ThemeColors]] = MappingProxyType(
    {
        ThemeId.DARK: DARK_THEME,
        ThemeId.LIGHT: LIGHT_THEME,
        ThemeId.WARM: WARM_THEME,
        ThemeId.WARM_LIGHT: WARM_LIGHT_THEME,
        ThemeId.MONOCHROME: MONOCHROME_THEME,
        ThemeId.MONOCHROME_LIGHT: MONOCHROME_LIGHT_THEME,
    }
)
DEFAULT_THEME = THEMES[ThemeId.DARK]


def colors_for_theme(theme_id: ThemeId) -> ThemeColors:
    """Resolve a stable preference identifier to immutable color roles."""
    return THEMES[theme_id]


FORM_FIELD_HORIZONTAL_PADDING = 10
SCROLLBAR_EXTENT = 14
SCROLLBAR_MINIMUM_HANDLE = 28




def build_stylesheet(colors: ThemeColors) -> str:
    """Compose original area rules in the exact pre-refactor cascade order."""
    return "".join(
        (
            widgets_styles(colors),
            sidebar_buttons_styles(colors),
            menus_styles(colors),
            toolbar_styles(colors),
            dialog_surfaces_styles(colors),
            tables_styles(colors),
            scrollbars_styles(
                colors,
                scrollbar_extent=SCROLLBAR_EXTENT,
                scrollbar_minimum_handle=SCROLLBAR_MINIMUM_HANDLE,
            ),
            filter_styles(colors),
            tree_styles(colors),
            tabs_styles(colors),
            start_styles(colors),
            surface_styles(colors, scrollbar_minimum_handle=SCROLLBAR_MINIMUM_HANDLE),
            search_styles(colors),
            messages_styles(colors),
            saved_logins_styles(colors),
            host_keys_styles(colors),
            ssh_controls_styles(colors),
            primary_buttons_styles(colors),
            secondary_buttons_styles(colors),
            danger_buttons_styles(colors),
            fields_styles(colors, form_field_horizontal_padding=FORM_FIELD_HORIZONTAL_PADDING),
            status_styles(colors),
            progress_styles(colors),
        )
    ).strip()
