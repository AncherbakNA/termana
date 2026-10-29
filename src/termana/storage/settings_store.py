"""Strict, atomic JSON persistence for application preferences."""

import json
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from termana.models.settings import (
    DEFAULT_APPLICATION_SETTINGS,
    DEFAULT_SSH_ENGINE,
    DEFAULT_TERMINAL_SCROLLBACK_LINES,
    MAX_TERMINAL_SCROLLBACK_LINES,
    MIN_TERMINAL_SCROLLBACK_LINES,
    ApplicationSettings,
    ClickEditGesture,
    InterfaceLanguage,
    LocalTerminalSettings,
    SshEngine,
    TerminalFontSettings,
    ThemeId,
    WindowLayoutSettings,
)
from termana.models.terminal_highlighting import TerminalHighlighting
from termana.models.terminal_layout import (
    TerminalPaneLayout,
    pane_layout_from_data,
    pane_layout_to_data,
)
from termana.storage.atomic_file import replace_file_atomically
from termana.storage.json_objects import reject_duplicate_json_keys
from termana.storage.settings_migration_backup import (
    SettingsMigrationBackupError,
    preserve_before_schema_change,
)

SETTINGS_SCHEMA_VERSION: Final = 15
MAX_SETTINGS_FILE_BYTES: Final = 96 * 1024
MAX_LAYOUT_BLOB_BYTES: Final = 16 * 1024
MAX_LAYOUT_BLOB_BASE64_CHARACTERS: Final = ((MAX_LAYOUT_BLOB_BYTES + 2) // 3) * 4
DOCUMENT_FIELDS: Final = frozenset({"schema_version", "settings"})
SETTINGS_FIELDS_V1: Final = frozenset({"theme"})
SETTINGS_FIELDS_WITH_LAYOUT: Final = frozenset({"theme", "window_layout"})
SETTINGS_FIELDS_V4: Final = SETTINGS_FIELDS_WITH_LAYOUT | {"terminal_font"}
SETTINGS_FIELDS_V5: Final = SETTINGS_FIELDS_V4 | {"terminal_scrollback_lines"}
SETTINGS_FIELDS_V6: Final = SETTINGS_FIELDS_V5 | {"visual_bell_enabled"}
SETTINGS_FIELDS_V8: Final = SETTINGS_FIELDS_V6 | {"sftp_auto_open"}
SETTINGS_FIELDS_V9: Final = SETTINGS_FIELDS_V8 | {"ssh_engine"}
SETTINGS_FIELDS_V10: Final = SETTINGS_FIELDS_V9 | {"interface_language"}
SETTINGS_FIELDS_V11: Final = SETTINGS_FIELDS_V10 | {"click_edit_gesture"}
SETTINGS_FIELDS_V12: Final = SETTINGS_FIELDS_V11 | {"terminal_highlighting"}
SETTINGS_FIELDS_V13: Final = SETTINGS_FIELDS_V12 | {"confirm_session_close"}
SETTINGS_FIELDS_V14: Final = SETTINGS_FIELDS_V13 - {"click_edit_gesture"}
SETTINGS_FIELDS_V15: Final = SETTINGS_FIELDS_V14 | {
    "local_terminal", "backspace_sends_ctrl_h", "toolbar_actions",
}
TERMINAL_FONT_FIELDS: Final = frozenset({"family", "point_size"})
WINDOW_LAYOUT_FIELDS_V2: Final = frozenset(
    {
        "geometry_base64",
        "main_splitter_state_base64",
        "sidebar_visible",
    }
)
WINDOW_LAYOUT_FIELDS_V3: Final = WINDOW_LAYOUT_FIELDS_V2 | {"main_window_state_base64"}
WINDOW_LAYOUT_FIELDS_V7: Final = WINDOW_LAYOUT_FIELDS_V3 | {"terminal_panes"}


class SettingsStoreError(RuntimeError):
    """Raised when application preferences cannot be read or saved safely."""


class SettingsStore:
    """Load and atomically save one application-settings snapshot."""

    def __init__(self, path: Path) -> None:
        """Store the target path without touching the filesystem."""
        self.path = path
        self._write_disabled_reason: str | None = None

    def load(self) -> ApplicationSettings:
        """Load preferences or return defaults when the file does not exist."""
        try:
            with self.path.open("rb") as settings_file:
                raw_document = settings_file.read(MAX_SETTINGS_FILE_BYTES + 1)
        except FileNotFoundError:
            self._write_disabled_reason = None
            return DEFAULT_APPLICATION_SETTINGS
        except OSError as error:
            raise SettingsStoreError(f"Could not read {self.path}: {error}") from error

        try:
            if len(raw_document) > MAX_SETTINGS_FILE_BYTES:
                raise ValueError("settings file exceeds the supported size limit")
            document = json.loads(
                raw_document.decode("utf-8"),
                object_pairs_hook=reject_duplicate_json_keys,
            )
        except (UnicodeError, ValueError, RecursionError) as error:
            raise SettingsStoreError(f"Could not read {self.path}: {error}") from error

        if (
            isinstance(document, dict)
            and type(document.get("schema_version")) is int
            and document["schema_version"] > SETTINGS_SCHEMA_VERSION
        ):
            self._write_disabled_reason = (
                "Settings were created by a newer Termana version and cannot be overwritten"
            )

        settings = self._settings_from_document(document)
        self._write_disabled_reason = None
        return settings

    def save(self, settings: ApplicationSettings) -> None:
        """Write a complete snapshot through a same-directory temporary file."""
        if self._write_disabled_reason is not None:
            raise SettingsStoreError(self._write_disabled_reason)
        if not isinstance(settings, ApplicationSettings):
            raise SettingsStoreError("Expected an ApplicationSettings snapshot")

        document = {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "settings": {
                "theme": settings.theme.value,
                "terminal_font": {
                    "family": settings.terminal_font.family,
                    "point_size": settings.terminal_font.point_size,
                },
                "terminal_scrollback_lines": settings.terminal_scrollback_lines,
                "visual_bell_enabled": settings.visual_bell_enabled,
                "confirm_session_close": settings.confirm_session_close,
                "sftp_auto_open": settings.sftp_auto_open,
                "ssh_engine": settings.ssh_engine.value,
                "interface_language": settings.interface_language.value,
                "terminal_highlighting": settings.terminal_highlighting.to_data(),
                "local_terminal": {
                    "directory": settings.local_terminal.directory,
                    "shell": settings.local_terminal.shell,
                },
                "backspace_sends_ctrl_h": settings.backspace_sends_ctrl_h,
                "toolbar_actions": list(settings.toolbar_actions),
                "window_layout": {
                    "geometry_base64": self._encode_layout_blob(settings.window_layout.geometry),
                    "main_window_state_base64": self._encode_layout_blob(
                        settings.window_layout.main_window_state
                    ),
                    "main_splitter_state_base64": self._encode_layout_blob(
                        settings.window_layout.main_splitter_state
                    ),
                    "sidebar_visible": settings.window_layout.sidebar_visible,
                    "terminal_panes": pane_layout_to_data(settings.window_layout.terminal_panes),
                },
            },
        }
        try:
            serialized = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(serialized) > MAX_SETTINGS_FILE_BYTES:
                raise ValueError("settings file exceeds the supported size limit")
        except (UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise SettingsStoreError(f"Could not write {self.path}: {error}") from error

        try:
            prepared = preserve_before_schema_change(
                self.path,
                current_version=SETTINGS_SCHEMA_VERSION,
                limit=MAX_SETTINGS_FILE_BYTES,
                validate=self._settings_from_document,
            )
            prepared.assert_unchanged()
            replace_file_atomically(self.path, serialized)
        except (OSError, SettingsMigrationBackupError) as error:
            raise SettingsStoreError(f"Could not write {self.path}: {error}") from error

    @classmethod
    def _settings_from_document(cls, document: Any) -> ApplicationSettings:
        """Validate the complete versioned document before constructing values."""
        if not isinstance(document, dict):
            raise SettingsStoreError("Settings file must contain a JSON object")
        if set(document) != DOCUMENT_FIELDS:
            raise SettingsStoreError("Settings file fields do not match its schema")

        schema_version = document.get("schema_version")
        if type(schema_version) is not int:
            raise SettingsStoreError("Unsupported settings file schema version")

        settings = document.get("settings")
        if not isinstance(settings, dict):
            raise SettingsStoreError("Settings file must contain a settings object")
        if schema_version == 1:
            return cls._settings_from_v1(settings)
        if schema_version == 2:
            return cls._settings_from_v2(settings)
        if schema_version == 3:
            return cls._settings_from_v3(settings)
        if schema_version == 4:
            return cls._settings_from_v4(settings)
        if schema_version == 5:
            return cls._settings_from_v5(settings)
        if schema_version == 6:
            return cls._settings_from_v6(settings)
        if schema_version == 7:
            return cls._settings_from_v7(settings)
        if schema_version == 8:
            return cls._settings_from_v8(settings)
        if schema_version == 9:
            return cls._settings_from_v9(settings)
        if schema_version == 10:
            return cls._settings_from_v10(settings)
        if schema_version == 11:
            return cls._settings_from_v11(settings)
        if schema_version == SETTINGS_SCHEMA_VERSION:
            if set(settings) != SETTINGS_FIELDS_V15:
                raise SettingsStoreError("Settings fields do not match schema version 15")
            local = settings["local_terminal"]
            if not isinstance(local, dict) or set(local) != {"directory", "shell"}:
                raise SettingsStoreError("Invalid local terminal settings")
            if type(settings["toolbar_actions"]) is not list:
                raise SettingsStoreError("Toolbar actions must be a list")
            previous = {key: value for key, value in settings.items()
                        if key not in {"local_terminal", "backspace_sends_ctrl_h", "toolbar_actions"}}
            try:
                return replace(
                    cls._settings_from_document({"schema_version": 14, "settings": previous}),
                    local_terminal=LocalTerminalSettings(**local),
                    backspace_sends_ctrl_h=settings["backspace_sends_ctrl_h"],
                    toolbar_actions=tuple(settings["toolbar_actions"]),
                )
            except (TypeError, ValueError) as error:
                raise SettingsStoreError("Invalid local terminal/input/toolbar settings") from error
        if schema_version == 14:
            if set(settings) != SETTINGS_FIELDS_V14:
                raise SettingsStoreError("Settings fields do not match schema version 14")
            # Reuse strict legacy validation, then discard the removed option.
            return cls._settings_from_document(
                {
                    "schema_version": 13,
                    "settings": dict(settings, click_edit_gesture="disabled"),
                }
            )
        if schema_version == 13:
            if set(settings) != SETTINGS_FIELDS_V13:
                raise SettingsStoreError("Settings fields do not match schema version 13")
            if type(settings["confirm_session_close"]) is not bool:
                raise SettingsStoreError("Confirm session close must be a boolean")
            previous = {
                key: value for key, value in settings.items() if key != "confirm_session_close"
            }
            return replace(
                cls._settings_from_document({"schema_version": 12, "settings": previous}),
                confirm_session_close=settings["confirm_session_close"],
            )
        if schema_version == 12:
            if set(settings) != SETTINGS_FIELDS_V12:
                raise SettingsStoreError("Settings fields do not match schema version 12")
            previous = {
                key: value for key, value in settings.items() if key != "terminal_highlighting"
            }
            try:
                highlighting = TerminalHighlighting.from_data(settings["terminal_highlighting"])
            except (TypeError, ValueError) as error:
                raise SettingsStoreError("Invalid terminal highlighting settings") from error
            return replace(cls._settings_from_v11(previous), terminal_highlighting=highlighting)
        raise SettingsStoreError("Unsupported settings file schema version")

    @staticmethod
    def _theme_from_settings(settings: dict[str, Any]) -> ThemeId:
        """Decode one stable theme identifier shared by all known schemas."""
        theme_value = settings.get("theme")
        if not isinstance(theme_value, str):
            raise SettingsStoreError("Theme identifier must be a string")
        try:
            return ThemeId(theme_value)
        except ValueError as error:
            raise SettingsStoreError("Unsupported theme identifier") from error

    @classmethod
    def _settings_from_v1(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Migrate the theme-only schema in memory without rewriting the file."""
        if set(settings) != SETTINGS_FIELDS_V1:
            raise SettingsStoreError("Settings fields do not match schema version 1")
        return ApplicationSettings(theme=cls._theme_from_settings(settings))

    @classmethod
    def _settings_from_v2(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Migrate geometry, splitter, and visibility without rewriting the file."""
        return cls._settings_with_layout(
            settings,
            schema_version=2,
            expected_layout_fields=WINDOW_LAYOUT_FIELDS_V2,
            has_main_window_state=False,
        )

    @classmethod
    def _settings_from_v3(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Migrate the complete pre-font layout schema without rewriting it."""
        return cls._settings_with_layout(
            settings,
            schema_version=3,
            expected_settings_fields=SETTINGS_FIELDS_WITH_LAYOUT,
            expected_layout_fields=WINDOW_LAYOUT_FIELDS_V3,
            has_main_window_state=True,
        )

    @classmethod
    def _settings_from_v4(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Migrate the pre-scrollback terminal-font schema without rewriting it."""
        return cls._settings_with_layout(
            settings,
            schema_version=4,
            expected_settings_fields=SETTINGS_FIELDS_V4,
            expected_layout_fields=WINDOW_LAYOUT_FIELDS_V3,
            has_main_window_state=True,
            has_terminal_font=True,
        )

    @classmethod
    def _settings_from_v5(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Migrate the pre-visual-bell schema in memory without rewriting it."""
        return cls._settings_with_layout(
            settings,
            schema_version=5,
            expected_settings_fields=SETTINGS_FIELDS_V5,
            expected_layout_fields=WINDOW_LAYOUT_FIELDS_V3,
            has_main_window_state=True,
            has_terminal_font=True,
            has_terminal_scrollback_lines=True,
        )

    @classmethod
    def _settings_from_v6(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Migrate the pre-pane-layout schema without rewriting the file."""
        return cls._settings_with_layout(
            settings,
            schema_version=6,
            expected_settings_fields=SETTINGS_FIELDS_V6,
            expected_layout_fields=WINDOW_LAYOUT_FIELDS_V3,
            has_main_window_state=True,
            has_terminal_font=True,
            has_terminal_scrollback_lines=True,
            has_visual_bell_enabled=True,
        )

    @classmethod
    def _settings_from_v7(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Migrate the pre-SFTP-auto-open schema without rewriting the file."""
        return cls._settings_with_layout(
            settings,
            schema_version=7,
            expected_settings_fields=SETTINGS_FIELDS_V6,
            expected_layout_fields=WINDOW_LAYOUT_FIELDS_V7,
            has_main_window_state=True,
            has_terminal_font=True,
            has_terminal_scrollback_lines=True,
            has_visual_bell_enabled=True,
            has_terminal_panes=True,
        )

    @classmethod
    def _settings_from_v8(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Decode the current schema with an explicit SFTP sidebar preference."""
        auto_open = settings.get("sftp_auto_open")
        if type(auto_open) is not bool:
            raise SettingsStoreError("SFTP auto-open must be a boolean")
        return cls._settings_with_layout(
            settings,
            schema_version=8,
            expected_settings_fields=SETTINGS_FIELDS_V8,
            expected_layout_fields=WINDOW_LAYOUT_FIELDS_V7,
            has_main_window_state=True,
            has_terminal_font=True,
            has_terminal_scrollback_lines=True,
            has_visual_bell_enabled=True,
            has_terminal_panes=True,
            sftp_auto_open=auto_open,
        )

    @classmethod
    def _settings_from_v9(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Preserve explicit engine choices; older schemas use the current product default."""
        if set(settings) != SETTINGS_FIELDS_V9:
            raise SettingsStoreError("Settings fields do not match schema version 9")
        engine = settings.get("ssh_engine")
        if type(engine) is not str:
            raise SettingsStoreError("SSH engine must be a string")
        try:
            selected = SshEngine(engine)
        except ValueError:
            raise SettingsStoreError("Unsupported SSH engine") from None
        auto_open = settings.get("sftp_auto_open")
        if type(auto_open) is not bool:
            raise SettingsStoreError("SFTP auto-open must be a boolean")
        return cls._settings_with_layout(
            settings,
            schema_version=9,
            expected_settings_fields=SETTINGS_FIELDS_V9,
            expected_layout_fields=WINDOW_LAYOUT_FIELDS_V7,
            has_main_window_state=True,
            has_terminal_font=True,
            has_terminal_scrollback_lines=True,
            has_visual_bell_enabled=True,
            has_terminal_panes=True,
            sftp_auto_open=auto_open,
            ssh_engine=selected,
        )

    @classmethod
    def _settings_from_v11(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Decode the gesture while retaining exact schema-10 validation."""
        if set(settings) != SETTINGS_FIELDS_V11:
            raise SettingsStoreError("Settings fields do not match schema version 11")
        gesture = settings["click_edit_gesture"]
        if type(gesture) is not str:
            raise SettingsStoreError("Click edit gesture must be a string")
        try:
            ClickEditGesture(gesture)
        except ValueError:
            raise SettingsStoreError("Unsupported click edit gesture") from None
        previous = {key: value for key, value in settings.items() if key != "click_edit_gesture"}
        return cls._settings_from_v10(previous)

    @classmethod
    def _settings_from_v10(cls, settings: dict[str, Any]) -> ApplicationSettings:
        """Add one validated UI language without relaxing the previous strict schema."""
        if set(settings) != SETTINGS_FIELDS_V10:
            raise SettingsStoreError("Settings fields do not match schema version 10")
        language = settings["interface_language"]
        if type(language) is not str:
            raise SettingsStoreError("Interface language must be a string")
        try:
            selected = InterfaceLanguage(language)
        except ValueError:
            raise SettingsStoreError("Unsupported interface language") from None
        previous = {key: value for key, value in settings.items() if key != "interface_language"}
        return replace(cls._settings_from_v9(previous), interface_language=selected)

    @classmethod
    def _settings_with_layout(
        cls,
        settings: dict[str, Any],
        *,
        schema_version: int,
        expected_settings_fields: frozenset[str] = SETTINGS_FIELDS_WITH_LAYOUT,
        expected_layout_fields: frozenset[str],
        has_main_window_state: bool,
        has_terminal_font: bool = False,
        has_terminal_scrollback_lines: bool = False,
        has_visual_bell_enabled: bool = False,
        has_terminal_panes: bool = False,
        sftp_auto_open: bool = True,
        ssh_engine: SshEngine = DEFAULT_SSH_ENGINE,
    ) -> ApplicationSettings:
        """Decode the common bounded layout structure for schemas 2 through 9."""
        if set(settings) != expected_settings_fields:
            raise SettingsStoreError(
                f"Settings fields do not match schema version {schema_version}"
            )

        window_layout = settings.get("window_layout")
        if not isinstance(window_layout, dict):
            raise SettingsStoreError("Window layout must be a JSON object")
        if set(window_layout) != expected_layout_fields:
            raise SettingsStoreError(
                f"Window layout fields do not match schema version {schema_version}"
            )

        sidebar_visible = window_layout.get("sidebar_visible")
        if type(sidebar_visible) is not bool:
            raise SettingsStoreError("Sidebar visibility must be a boolean")

        try:
            layout = WindowLayoutSettings(
                geometry=cls._decode_layout_blob(
                    window_layout.get("geometry_base64"),
                    field_name="window geometry",
                ),
                main_window_state=cls._decode_layout_blob(
                    window_layout.get("main_window_state_base64"),
                    field_name="main window state",
                )
                if has_main_window_state
                else None,
                main_splitter_state=cls._decode_layout_blob(
                    window_layout.get("main_splitter_state_base64"),
                    field_name="main splitter state",
                ),
                sidebar_visible=sidebar_visible,
                terminal_panes=pane_layout_from_data(window_layout.get("terminal_panes"))
                if has_terminal_panes
                else TerminalPaneLayout(),
            )
        except (TypeError, ValueError) as error:
            raise SettingsStoreError(f"Invalid window layout: {error}") from error

        terminal_font = (
            cls._terminal_font_from_settings(settings)
            if has_terminal_font
            else TerminalFontSettings()
        )
        terminal_scrollback_lines = (
            cls._terminal_scrollback_lines_from_settings(settings)
            if has_terminal_scrollback_lines
            else DEFAULT_TERMINAL_SCROLLBACK_LINES
        )
        visual_bell_enabled = (
            cls._visual_bell_enabled_from_settings(settings)
            if has_visual_bell_enabled
            else DEFAULT_APPLICATION_SETTINGS.visual_bell_enabled
        )
        return ApplicationSettings(
            theme=cls._theme_from_settings(settings),
            terminal_font=terminal_font,
            terminal_scrollback_lines=terminal_scrollback_lines,
            visual_bell_enabled=visual_bell_enabled,
            sftp_auto_open=sftp_auto_open,
            ssh_engine=ssh_engine,
            window_layout=layout,
        )

    @staticmethod
    def _terminal_font_from_settings(settings: dict[str, Any]) -> TerminalFontSettings:
        """Decode one portable font preference without consulting installed fonts."""
        terminal_font = settings.get("terminal_font")
        if not isinstance(terminal_font, dict):
            raise SettingsStoreError("Terminal font must be a JSON object")
        if set(terminal_font) != TERMINAL_FONT_FIELDS:
            raise SettingsStoreError("Terminal font fields do not match the settings schema")

        try:
            return TerminalFontSettings(
                family=terminal_font.get("family"),
                point_size=terminal_font.get("point_size"),
            )
        except (TypeError, ValueError) as error:
            raise SettingsStoreError(f"Invalid terminal font: {error}") from error

    @staticmethod
    def _terminal_scrollback_lines_from_settings(settings: dict[str, Any]) -> int:
        """Decode one bounded physical-row history capacity."""
        terminal_scrollback_lines = settings.get("terminal_scrollback_lines")
        if type(terminal_scrollback_lines) is not int:
            raise SettingsStoreError("Terminal scrollback lines must be an integer")
        if not (
            MIN_TERMINAL_SCROLLBACK_LINES
            <= terminal_scrollback_lines
            <= MAX_TERMINAL_SCROLLBACK_LINES
        ):
            raise SettingsStoreError(
                "Terminal scrollback lines must be between "
                f"{MIN_TERMINAL_SCROLLBACK_LINES} and {MAX_TERMINAL_SCROLLBACK_LINES}"
            )
        return terminal_scrollback_lines

    @staticmethod
    def _visual_bell_enabled_from_settings(settings: dict[str, Any]) -> bool:
        """Decode an exact boolean without accepting JSON numbers as booleans."""
        visual_bell_enabled = settings.get("visual_bell_enabled")
        if type(visual_bell_enabled) is not bool:
            raise SettingsStoreError("Visual bell enabled must be a boolean")
        return visual_bell_enabled

    @staticmethod
    def _encode_layout_blob(value: bytes | None) -> str | None:
        """Encode an opaque Qt state with a strict per-value size limit."""
        if value is None:
            return None
        if type(value) is not bytes or not value:
            raise SettingsStoreError("Window layout state must contain non-empty bytes")
        if len(value) > MAX_LAYOUT_BLOB_BYTES:
            raise SettingsStoreError("Window layout state exceeds the supported size limit")
        return b64encode(value).decode("ascii")

    @staticmethod
    def _decode_layout_blob(value: Any, *, field_name: str) -> bytes | None:
        """Decode canonical Base64 without allowing unbounded intermediate data."""
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise SettingsStoreError(f"{field_name} must be Base64 text or null")
        if len(value) > MAX_LAYOUT_BLOB_BASE64_CHARACTERS:
            raise SettingsStoreError(f"{field_name} exceeds the supported size limit")
        try:
            decoded = b64decode(value.encode("ascii"), validate=True)
        except (BinasciiError, UnicodeError, ValueError) as error:
            raise SettingsStoreError(f"{field_name} is not valid Base64") from error
        if not decoded or len(decoded) > MAX_LAYOUT_BLOB_BYTES:
            raise SettingsStoreError(f"{field_name} has an unsupported size")
        if b64encode(decoded).decode("ascii") != value:
            raise SettingsStoreError(f"{field_name} is not canonical Base64")
        return decoded
