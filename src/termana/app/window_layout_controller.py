"""Restore and capture the window's existing connection-free layout lifecycle."""

from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QByteArray

from termana.models.settings import ApplicationSettings, WindowLayoutSettings
from termana.models.terminal_layout import TerminalPaneLayout
from termana.storage.settings_store import SettingsStoreError


class WindowLayoutWindow(Protocol):
    """Opaque Qt geometry/state, without menus, sessions or close decisions."""

    def restoreGeometry(self, state: QByteArray) -> bool: ...

    def restoreState(self, state: QByteArray, version: int) -> bool: ...

    def resize(self, width: int, height: int) -> None: ...

    def saveGeometry(self) -> QByteArray: ...

    def saveState(self, version: int) -> QByteArray: ...


class WindowLayoutSplitter(Protocol):
    """The existing main splitter's opaque state and default proportions."""

    def restoreState(self, state: QByteArray) -> bool: ...

    def setSizes(self, sizes: list[int]) -> None: ...

    def saveState(self) -> QByteArray: ...


class WindowLayoutSidebar(Protocol):
    """The View action remains the source of sidebar visibility."""

    def setChecked(self, checked: bool) -> None: ...

    def isChecked(self) -> bool: ...


class WindowLayoutPanes(Protocol):
    """Use only public, connection-free pane layout snapshots."""

    def restore_pane_layout(self, layout: TerminalPaneLayout) -> bool: ...

    def capture_pane_layout(self) -> TerminalPaneLayout: ...


class WindowLayoutSettingsOwner(Protocol):
    """SettingsController retains startup-corruption and persistence policy."""

    @property
    def settings(self) -> ApplicationSettings: ...

    def persist_window_layout(self, layout: WindowLayoutSettings) -> bool: ...


class WindowLayoutController:
    """Coordinate startup restoration and one close-time capture, not shutdown."""

    def __init__(
        self,
        window: WindowLayoutWindow,
        splitter: WindowLayoutSplitter,
        sidebar: WindowLayoutSidebar,
        panes: WindowLayoutPanes,
        settings: WindowLayoutSettingsOwner,
        *,
        default_window_size: tuple[int, int],
        default_sidebar_width: int,
        state_version: int,
        warn: Callable[[str], None],
    ) -> None:
        self._window = window
        self._splitter = splitter
        self._sidebar = sidebar
        self._panes = panes
        self._settings = settings
        self._default_window_size = default_window_size
        self._default_sidebar_width = default_sidebar_width
        self._state_version = state_version
        self._warn = warn
        self._save_attempted = False
        self._needs_initial_fit = True

    @property
    def needs_initial_fit(self) -> bool:
        """Only an unsaved/default geometry may be fitted at first exposure."""
        return self._needs_initial_fit

    def restore(self) -> None:
        """Restore independent parts in order, keeping their existing fallbacks."""
        layout = self._settings.settings.window_layout
        self._needs_initial_fit = layout.geometry is None
        if layout.geometry is not None:
            restored = self._window.restoreGeometry(QByteArray(layout.geometry))
            self._needs_initial_fit = not restored
            if not restored:
                self._window.resize(*self._default_window_size)
                self._warn("Could not restore the saved main-window geometry")

        if layout.main_window_state is not None and not self._window.restoreState(
            QByteArray(layout.main_window_state), self._state_version
        ):
            self._warn("Could not restore the saved main-window state")

        if layout.main_splitter_state is not None and not self._splitter.restoreState(
            QByteArray(layout.main_splitter_state)
        ):
            workspace_width = self._default_window_size[0] - self._default_sidebar_width
            self._splitter.setSizes([self._default_sidebar_width, workspace_width])
            self._warn("Could not restore the saved main-splitter state")

        self._sidebar.setChecked(layout.sidebar_visible)
        if not self._panes.restore_pane_layout(layout.terminal_panes):
            self._warn("Could not restore the saved terminal-pane layout")

    def persist_once(self) -> None:
        """Attempt one snapshot before shutdown, including a rejected/failed save."""
        if self._save_attempted:
            return
        self._save_attempted = True
        layout = WindowLayoutSettings(
            geometry=bytes(self._window.saveGeometry()),
            main_window_state=bytes(self._window.saveState(self._state_version)),
            main_splitter_state=bytes(self._splitter.saveState()),
            sidebar_visible=self._sidebar.isChecked(),
            terminal_panes=self._panes.capture_pane_layout(),
        )
        try:
            self._settings.persist_window_layout(layout)
        except SettingsStoreError as error:
            self._warn(f"Could not save the window layout: {error}")
