"""Typed user preferences independent of Qt widgets and persistence."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final
from unicodedata import category, normalize

from termana.models.terminal_highlighting import TerminalHighlighting
from termana.models.terminal_layout import TerminalPaneLayout

MIN_TERMINAL_FONT_POINT_SIZE: Final = 8
MAX_TERMINAL_FONT_POINT_SIZE: Final = 72
DEFAULT_TERMINAL_FONT_POINT_SIZE: Final = 13
MAX_TERMINAL_FONT_FAMILY_CHARACTERS: Final = 128
MIN_TERMINAL_SCROLLBACK_LINES: Final = 0
MAX_TERMINAL_SCROLLBACK_LINES: Final = 100_000
DEFAULT_TERMINAL_SCROLLBACK_LINES: Final = 5_000
TOOLBAR_ACTION_IDS: Final = (
    "new_connection_action", "toggle_sidebar_action", "local_terminal_action",
    "pane_layout_action", "saved_commands_action", "switch_terminal_action", "settings_action",
)


@dataclass(frozen=True, slots=True)
class LocalTerminalSettings:
    """Literal portable paths; availability is checked only on the current host."""

    directory: str | None = None
    shell: str | None = None

    def __post_init__(self) -> None:
        for name in ("directory", "shell"):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) is not str:
                raise TypeError(f"Local {name} must be text or None")
            if (not value or len(value) > 4096
                    or any(category(c).startswith("C") for c in value)
                    or not (PurePosixPath(value).is_absolute()
                            or PureWindowsPath(value).is_absolute())):
                raise ValueError(f"Local {name} must be a literal absolute path (up to4096 characters)")


class SshEngine(StrEnum):
    """Explicit preference for the next application launch, not a live reroute."""

    SYSTEM = "system"
    EMBEDDED = "embedded"


DEFAULT_SSH_ENGINE: Final = SshEngine.EMBEDDED


class InterfaceLanguage(StrEnum):
    """Explicit next-launch UI language, independent of shell/OS encoding."""

    ENGLISH = "en"
    RUSSIAN = "ru"


class ClickEditGesture(StrEnum):
    """Legacy schema 11-13 values only; click placement is no longer a feature."""

    ALT = "alt"
    PRIMARY_ALT = "primary_alt"
    PRIMARY = "primary"
    DISABLED = "disabled"


class ThemeId(StrEnum):
    """Stable identifiers stored instead of presentation colors or labels."""

    DARK = "dark"
    LIGHT = "light"
    WARM = "warm"
    WARM_LIGHT = "warm_light"
    MONOCHROME = "monochrome"
    MONOCHROME_LIGHT = "monochrome_light"


@dataclass(frozen=True, slots=True)
class TerminalFontSettings:
    """Portable terminal font choice independent of installed Qt fonts."""

    family: str | None = None
    point_size: int = DEFAULT_TERMINAL_FONT_POINT_SIZE

    def __post_init__(self) -> None:
        """Validate portable values without requiring a font from this computer."""
        if self.family is not None:
            if not isinstance(self.family, str):
                raise TypeError("font family must be a string or None")
            if not self.family or self.family != self.family.strip():
                raise ValueError("font family must be a non-empty trimmed string")
            if len(self.family) > MAX_TERMINAL_FONT_FAMILY_CHARACTERS:
                raise ValueError("font family exceeds the supported length")
            if any(category(character).startswith("C") for character in self.family):
                raise ValueError("font family contains unsupported characters")
            object.__setattr__(self, "family", normalize("NFC", self.family))

        if type(self.point_size) is not int:
            raise TypeError("font point_size must be an int")
        if not MIN_TERMINAL_FONT_POINT_SIZE <= self.point_size <= MAX_TERMINAL_FONT_POINT_SIZE:
            raise ValueError(
                "font point_size must be between "
                f"{MIN_TERMINAL_FONT_POINT_SIZE} and {MAX_TERMINAL_FONT_POINT_SIZE}"
            )


@dataclass(frozen=True, slots=True)
class WindowLayoutSettings:
    """Opaque Qt layout state plus stable user-facing visibility choices."""

    geometry: bytes | None = None
    main_window_state: bytes | None = None
    main_splitter_state: bytes | None = None
    sidebar_visible: bool = True
    terminal_panes: TerminalPaneLayout = field(default_factory=TerminalPaneLayout)

    def __post_init__(self) -> None:
        """Keep Qt objects and malformed state out of the settings model."""
        for field_name in ("geometry", "main_window_state", "main_splitter_state"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not bytes:
                raise TypeError(f"{field_name} must be bytes or None")
            if value == b"":
                raise ValueError(f"{field_name} must not be empty")
        if type(self.sidebar_visible) is not bool:
            raise TypeError("sidebar_visible must be a bool")
        if type(self.terminal_panes) is not TerminalPaneLayout:
            raise TypeError("terminal_panes must be TerminalPaneLayout")


@dataclass(frozen=True, slots=True)
class ApplicationSettings:
    """Preferences that apply to the whole desktop application."""

    theme: ThemeId = ThemeId.DARK
    terminal_font: TerminalFontSettings = field(default_factory=TerminalFontSettings)
    window_layout: WindowLayoutSettings = field(default_factory=WindowLayoutSettings)
    terminal_scrollback_lines: int = DEFAULT_TERMINAL_SCROLLBACK_LINES
    visual_bell_enabled: bool = True
    confirm_session_close: bool = False
    sftp_auto_open: bool = True
    ssh_engine: SshEngine = DEFAULT_SSH_ENGINE
    interface_language: InterfaceLanguage = InterfaceLanguage.ENGLISH
    terminal_highlighting: TerminalHighlighting = field(default_factory=TerminalHighlighting)
    local_terminal: LocalTerminalSettings = field(default_factory=LocalTerminalSettings)
    backspace_sends_ctrl_h: bool = False
    toolbar_actions: tuple[str, ...] = TOOLBAR_ACTION_IDS

    def __post_init__(self) -> None:
        """Reject untyped values before they reach storage or presentation."""
        if not isinstance(self.theme, ThemeId):
            raise TypeError("theme must be a ThemeId")
        if not isinstance(self.terminal_font, TerminalFontSettings):
            raise TypeError("terminal_font must be TerminalFontSettings")
        if type(self.terminal_scrollback_lines) is not int:
            raise TypeError("terminal_scrollback_lines must be an int")
        if not (
            MIN_TERMINAL_SCROLLBACK_LINES
            <= self.terminal_scrollback_lines
            <= MAX_TERMINAL_SCROLLBACK_LINES
        ):
            raise ValueError(
                "terminal_scrollback_lines must be between "
                f"{MIN_TERMINAL_SCROLLBACK_LINES} and {MAX_TERMINAL_SCROLLBACK_LINES}"
            )
        if type(self.visual_bell_enabled) is not bool:
            raise TypeError("visual_bell_enabled must be a bool")
        if type(self.confirm_session_close) is not bool:
            raise TypeError("confirm_session_close must be a bool")
        if type(self.sftp_auto_open) is not bool:
            raise TypeError("sftp_auto_open must be a bool")
        if type(self.ssh_engine) is not SshEngine:
            raise TypeError("ssh_engine must be a SshEngine")
        if type(self.interface_language) is not InterfaceLanguage:
            raise TypeError("interface_language must be an InterfaceLanguage")
        if type(self.terminal_highlighting) is not TerminalHighlighting:
            raise TypeError("terminal_highlighting must be TerminalHighlighting")
        if not isinstance(self.window_layout, WindowLayoutSettings):
            raise TypeError("window_layout must be WindowLayoutSettings")
        if type(self.local_terminal) is not LocalTerminalSettings:
            raise TypeError("local_terminal must be LocalTerminalSettings")
        if type(self.backspace_sends_ctrl_h) is not bool:
            raise TypeError("backspace_sends_ctrl_h must be a bool")
        if (type(self.toolbar_actions) is not tuple
                or any(type(key) is not str or key not in TOOLBAR_ACTION_IDS
                       for key in self.toolbar_actions)
                or len(set(self.toolbar_actions)) != len(self.toolbar_actions)):
            raise ValueError("toolbar_actions must contain unique supported action identifiers")


DEFAULT_APPLICATION_SETTINGS: Final = ApplicationSettings()
