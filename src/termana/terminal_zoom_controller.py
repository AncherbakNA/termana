"""Persist terminal zoom without owning widgets, shortcuts or terminal sessions."""

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from termana.i18n import tr
from termana.models.settings import (
    DEFAULT_TERMINAL_FONT_POINT_SIZE,
    MAX_TERMINAL_FONT_POINT_SIZE,
    MIN_TERMINAL_FONT_POINT_SIZE,
    ApplicationSettings,
)
from termana.storage.settings_store import SettingsStoreError
from termana.terminal.zoom import ZoomAction


class TerminalZoomSettings(Protocol):
    """Read the latest settings; commit remains the save-before-apply boundary."""

    @property
    def settings(self) -> ApplicationSettings: ...

    def commit(self, candidate: ApplicationSettings) -> None: ...


class TerminalZoomController:
    """Coordinate bounded font-size changes through the existing settings owner."""

    def __init__(
        self,
        settings: TerminalZoomSettings,
        *,
        is_closing: Callable[[], bool],
        show_message: Callable[[str, int], None],
        confirmation_timeout_ms: int,
    ) -> None:
        self._settings = settings
        self._is_closing = is_closing
        self._show_message = show_message
        self._confirmation_timeout_ms = confirmation_timeout_ms

    def increase(self, _checked: bool = False) -> None:
        self._change_size(1)

    def decrease(self, _checked: bool = False) -> None:
        self._change_size(-1)

    def reset(self, _checked: bool = False) -> None:
        self._set_size(DEFAULT_TERMINAL_FONT_POINT_SIZE)

    def handle_request(self, action: object) -> None:
        """Accept only the application-owned zoom intents, never terminal input."""
        if action is ZoomAction.INCREASE:
            self.increase()
        elif action is ZoomAction.DECREASE:
            self.decrease()
        elif action is ZoomAction.RESET:
            self.reset()

    def _change_size(self, step: int) -> None:
        current_size = self._settings.settings.terminal_font.point_size
        self._set_size(
            min(
                MAX_TERMINAL_FONT_POINT_SIZE,
                max(MIN_TERMINAL_FONT_POINT_SIZE, current_size + step),
            )
        )

    def _set_size(self, point_size: int) -> None:
        if self._is_closing():
            return
        current = self._settings.settings
        if point_size == current.terminal_font.point_size:
            return
        candidate = replace(
            current,
            terminal_font=replace(current.terminal_font, point_size=point_size),
        )
        try:
            self._settings.commit(candidate)
        except SettingsStoreError as error:
            self._show_message(tr("Could not save terminal font size: {p0}", p0=f"{error}"), 0)
            return
        self._show_message(
            tr("Terminal font size set to {p0} pt.", p0=f"{point_size}"),
            self._confirmation_timeout_ms,
        )
