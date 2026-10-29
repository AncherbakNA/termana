"""Application-scoped coordination of persisted and live settings."""

import sys
from dataclasses import replace
from typing import Protocol

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from termana.models.settings import (
    DEFAULT_APPLICATION_SETTINGS,
    ApplicationSettings,
    ThemeId,
    WindowLayoutSettings,
)
from termana.theme import ThemeColors, build_stylesheet, colors_for_theme
from termana.windows_titlebar import create_titlebar_controller


class SettingsWriter(Protocol):
    """Small persistence boundary needed by the live controller."""

    def save(self, settings: ApplicationSettings) -> None:
        """Persist one complete immutable snapshot."""


class SettingsController(QObject):
    """Keep saved settings separate from a reversible, color-only preview."""

    theme_changed = Signal(object)
    terminal_font_changed = Signal(object)
    terminal_scrollback_lines_changed = Signal(int)
    visual_bell_enabled_changed = Signal(bool)
    confirm_session_close_changed = Signal(bool)
    sftp_auto_open_changed = Signal(bool)
    terminal_highlighting_changed = Signal(object)
    local_terminal_changed = Signal(object)
    backspace_sends_ctrl_h_changed = Signal(bool)
    toolbar_actions_changed = Signal(object)

    def __init__(
        self,
        application: QApplication,
        *,
        initial_settings: ApplicationSettings = DEFAULT_APPLICATION_SETTINGS,
        store: SettingsWriter | None = None,
        automatic_persistence_enabled: bool = True,
        parent: QObject | None = None,
    ) -> None:
        """Apply an already-loaded snapshot without writing it again."""
        super().__init__(parent)
        if not isinstance(initial_settings, ApplicationSettings):
            raise TypeError("initial_settings must be an ApplicationSettings snapshot")
        if type(automatic_persistence_enabled) is not bool:
            raise TypeError("automatic_persistence_enabled must be a bool")
        self._application = application
        self._store = store
        self._automatic_persistence_enabled = automatic_persistence_enabled
        self._settings = initial_settings
        self._preview_theme: ThemeId | None = None
        self._colors = colors_for_theme(initial_settings.theme)
        self._apply_cocoa_appearance()
        stylesheet = build_stylesheet(self._colors)
        if self._application.styleSheet() != stylesheet:
            self._application.setStyleSheet(stylesheet)
        self._titlebar_controller = create_titlebar_controller(
            application, self._colors, parent=self
        )

    @property
    def settings(self) -> ApplicationSettings:
        """Return the committed snapshot, never an unsaved theme preview."""
        return self._settings

    @property
    def displayed_theme(self) -> ThemeId:
        """Return the visible theme, including a temporary Preferences preview."""
        return self._preview_theme if self._preview_theme is not None else self._settings.theme

    @property
    def colors(self) -> ThemeColors:
        """Return the currently applied immutable color roles."""
        return self._colors

    def preview_theme(self, theme: ThemeId) -> None:
        """Recolor without saving or applying any other pending preferences."""
        if not isinstance(theme, ThemeId):
            raise TypeError("theme must be a ThemeId")
        self._preview_theme = theme
        self._apply_displayed_theme()

    def clear_theme_preview(self) -> None:
        """Return to the latest committed theme without undoing unrelated saves."""
        self._preview_theme = None
        self._apply_displayed_theme()

    def commit(self, candidate: ApplicationSettings) -> None:
        """Explicitly save, apply, and unlock future automatic persistence."""
        self._persist_and_apply(candidate, finish_theme_preview=True)
        self._automatic_persistence_enabled = True

    def persist_window_layout(self, layout: WindowLayoutSettings) -> bool:
        """Save runtime layout unless startup corruption disabled automatic writes."""
        if not isinstance(layout, WindowLayoutSettings):
            raise TypeError("layout must be WindowLayoutSettings")
        if not self._automatic_persistence_enabled:
            return False
        self._persist_and_apply(replace(self._settings, window_layout=layout))
        return True

    def _persist_and_apply(
        self, candidate: ApplicationSettings, *, finish_theme_preview: bool = False
    ) -> None:
        """Persist one complete snapshot before changing observable state."""
        if not isinstance(candidate, ApplicationSettings):
            raise TypeError("candidate must be an ApplicationSettings snapshot")
        if self._store is not None:
            self._store.save(candidate)

        previous = self._settings
        self._settings = candidate
        if finish_theme_preview:
            self._preview_theme = None
        self._apply_displayed_theme()
        if candidate.terminal_font != previous.terminal_font:
            self.terminal_font_changed.emit(candidate.terminal_font)
        if candidate.terminal_scrollback_lines != previous.terminal_scrollback_lines:
            self.terminal_scrollback_lines_changed.emit(candidate.terminal_scrollback_lines)
        if candidate.visual_bell_enabled != previous.visual_bell_enabled:
            self.visual_bell_enabled_changed.emit(candidate.visual_bell_enabled)
        if candidate.confirm_session_close != previous.confirm_session_close:
            self.confirm_session_close_changed.emit(candidate.confirm_session_close)
        if candidate.sftp_auto_open != previous.sftp_auto_open:
            self.sftp_auto_open_changed.emit(candidate.sftp_auto_open)
        if candidate.terminal_highlighting != previous.terminal_highlighting:
            self.terminal_highlighting_changed.emit(candidate.terminal_highlighting)
        if candidate.local_terminal != previous.local_terminal:
            self.local_terminal_changed.emit(candidate.local_terminal)
        if candidate.backspace_sends_ctrl_h != previous.backspace_sends_ctrl_h:
            self.backspace_sends_ctrl_h_changed.emit(candidate.backspace_sends_ctrl_h)
        if candidate.toolbar_actions != previous.toolbar_actions:
            self.toolbar_actions_changed.emit(candidate.toolbar_actions)

    def _apply_displayed_theme(self) -> None:
        """Publish only a genuine visual change; never reset fonts or sessions."""
        colors = colors_for_theme(self.displayed_theme)
        if colors is self._colors:
            return
        self._colors = colors
        self._apply_cocoa_appearance()
        self._application.setStyleSheet(build_stylesheet(colors))
        if self._titlebar_controller is not None:
            self._titlebar_controller.set_colors(colors)
        self.theme_changed.emit(colors)

    def _apply_cocoa_appearance(self) -> None:
        """Align native Mac chrome with this app's theme, not the OS preference."""
        if sys.platform == "darwin":
            rgb = self._colors.surface.lstrip("#")
            dark = sum(int(rgb[i:i+2], 16) for i in (0, 2, 4)) < 384
            self._application.styleHints().setColorScheme(
                Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light
            )
