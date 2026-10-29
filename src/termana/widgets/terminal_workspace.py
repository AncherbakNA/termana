"""Tabbed workspace for terminal sessions."""

from typing import Final
from uuid import UUID, uuid4
from weakref import ref

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QFont, QKeyEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from termana.connection_colors import connection_icon_color
from termana.dialogs.terminal_tab_title_dialog import TerminalTabTitleDialog
from termana.i18n import tr
from termana.models.connection import ConnectionProfile, ConnectionProtocol, WordDeleteMode
from termana.models.open_terminal import OpenTerminalSession, OpenTerminalState
from termana.models.settings import (
    DEFAULT_TERMINAL_SCROLLBACK_LINES,
    MAX_TERMINAL_SCROLLBACK_LINES,
    MIN_TERMINAL_SCROLLBACK_LINES,
    LocalTerminalSettings,
    TerminalFontSettings,
)
from termana.models.ssh_negotiation import SshNegotiatedFacts
from termana.models.ssh_session_status import SshSessionStatus
from termana.models.terminal_highlighting import TerminalHighlighting
from termana.models.terminal_layout import TerminalPaneLayout
from termana.models.terminal_multi_search import (
    MAX_MULTI_SEARCH_CELLS,
    MAX_MULTI_SEARCH_ROWS,
    MAX_MULTI_SEARCH_SESSIONS,
    TerminalSearchCapture,
    TerminalSearchDocument,
    TerminalSearchSource,
)
from termana.qt_lifecycle import schedule_delete_later
from termana.rdp.page import RdpPage
from termana.terminal.backend import PROCESS_EXIT_SIGNAL_TYPE, TerminalBackend
from termana.terminal.backend_factory import TerminalBackendFactory, create_terminal_backend
from termana.terminal.command import ShellCommand
from termana.terminal.local_preferences import local_shell_command
from termana.terminal.screen import TerminalTextSnapshot
from termana.terminal.tab_shortcuts import (
    TerminalTabAction,
    terminal_tab_action_for_key_event,
)
from termana.terminal.title import sanitize_terminal_title
from termana.terminal.zoom import zoom_action_for_key_event
from termana.theme import DEFAULT_THEME, TerminalColors, ThemeColors
from termana.ui_icons import interface_icon
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.inline_terminal_prompt import InlineTerminalPromptPage
from termana.widgets.terminal_page import TerminalPage
from termana.widgets.terminal_panes import TerminalPaneManager
from termana.widgets.wrapping_label import WrappingLabel

START_TAB_TITLE: Final = "Start"
LOCAL_TAB_TITLE_PREFIX: Final = "Local"


def _terminal_page_state(page: TerminalPage) -> OpenTerminalState:
    if page.reconnect_request_pending:
        return OpenTerminalState.RECONNECTING
    if page.ssh_secret_tab_label is not None:
        return (
            OpenTerminalState.PASSWORD
            if page.ssh_secret_tab_label == "Password"
            else OpenTerminalState.AUTHENTICATION
        )
    return OpenTerminalState.ENDED if page.session_ended else OpenTerminalState.OPEN


class _TerminalShortcutEventFilter(QObject):
    """Handle fallback shortcuts only on terminal-owned input surfaces."""

    def __init__(
        self,
        workspace: "TerminalWorkspace",
    ) -> None:
        super().__init__(workspace)
        self._workspace_ref = ref(workspace)
        self._attached = True

    def watch(self, widget: QWidget) -> None:
        """Observe one strongly owned terminal input without touching QApplication."""
        if not isinstance(widget, QWidget):
            raise TypeError("shortcut target must be a QWidget")
        widget.installEventFilter(self)

    def attach(self) -> None:
        """Enable fallback handling when a reusable workspace is shown again."""
        self._attached = True

    def detach(self) -> None:
        """Disable every watched surface without changing Qt object ownership."""
        self._attached = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Forward matching local events only while the weak owner remains valid."""
        if not self._attached:
            return False
        workspace_ref = self._workspace_ref
        workspace = None if workspace_ref is None else workspace_ref()
        if workspace is None or not isValid(workspace):
            return False
        return workspace._filter_terminal_shortcut_event(watched, event)


def _qt_tab_label(title: str) -> str:
    """Escape Qt's ampersand mnemonic syntax in untrusted tab text."""
    return title.replace("&", "&&")


def _terminal_page_tab_label(
    page: TerminalPage,
    *,
    attention_requested: bool = False,
) -> str:
    """Keep tab text name-only; state belongs in the icon and description."""
    if not isinstance(attention_requested, bool):
        raise TypeError("attention_requested must be a boolean")
    return _qt_tab_label(page.display_title)


def _terminal_page_tab_description(
    page: TerminalPage,
    *,
    attention_requested: bool = False,
) -> str:
    """Describe tab state explicitly for tooltips and assistive technology."""
    if not isinstance(attention_requested, bool):
        raise TypeError("attention_requested must be a boolean")
    title = page.display_title
    if page.remote_window_title and page.remote_window_title != page.terminal_name:
        title = tr("{p0} · Remote title: {p1}", p0=title, p1=page.remote_window_title)
    if page.custom_tab_title is not None:
        title = tr("{p0} · Session: {p1}", p0=f"{title}", p1=f"{page.terminal_name}")
    marker = tr(page.ssh_secret_tab_label) if page.ssh_secret_tab_label is not None else None
    if marker is not None:
        return tr("{p0} requested — {p1}", p0=f"{marker}", p1=f"{title}")
    if attention_requested:
        return tr("Terminal bell — {p0}", p0=f"{title}")
    return title


def _validated_terminal_scrollback_lines(value: object) -> int:
    """Return one application-supported history capacity or raise."""
    if type(value) is not int:
        raise TypeError("terminal_scrollback_lines must be an int")
    if not MIN_TERMINAL_SCROLLBACK_LINES <= value <= MAX_TERMINAL_SCROLLBACK_LINES:
        raise ValueError(
            "terminal_scrollback_lines must be between "
            f"{MIN_TERMINAL_SCROLLBACK_LINES} and {MAX_TERMINAL_SCROLLBACK_LINES}"
        )
    return value


class TerminalWorkspace(QFrame):
    """Own and display the application's terminal tabs."""

    new_connection_requested = Signal()
    local_terminal_requested = Signal()
    local_terminal_opened = Signal(str)
    local_terminal_error = Signal(str)
    terminal_closed = Signal(str)
    terminal_count_changed = Signal(int)
    active_terminal_changed = Signal()
    open_sessions_changed = Signal()
    pane_layout_changed = Signal()
    command_terminal_finished = Signal(object, bytes, object, PROCESS_EXIT_SIGNAL_TYPE)
    command_terminal_started = Signal(object, object, int)
    command_terminal_closing = Signal(object, object)
    ssh_login_submitted = Signal(object, object, str)
    ssh_secret_submitted = Signal(object, object, str)
    ssh_secret_cancel_requested = Signal(object, object)
    connection_reconnect_requested = Signal(object, object)
    terminal_zoom_requested = Signal(object)
    output_export_requested = Signal(object)
    saved_commands_requested = Signal(object)
    ssh_session_details_requested = Signal(object)
    save_ssh_session_requested = Signal(object)
    all_terminals_stopped = Signal()

    def __init__(
        self,
        backend_factory: TerminalBackendFactory | None = None,
        *,
        terminal_colors: TerminalColors | None = None,
        terminal_font: TerminalFontSettings | None = None,
        terminal_scrollback_lines: int = DEFAULT_TERMINAL_SCROLLBACK_LINES,
        visual_bell_enabled: bool = True,
        confirm_session_close: bool = False,
    ) -> None:
        """Build the tab widget and its initial empty page."""
        super().__init__()

        self.setObjectName("terminalWorkspace")
        self._backend_factory = backend_factory or create_terminal_backend
        self.set_confirm_session_close(confirm_session_close)
        self._terminal_colors = terminal_colors or DEFAULT_THEME.terminal
        self._interface_colors = DEFAULT_THEME
        self._terminal_font_settings = (
            terminal_font if terminal_font is not None else TerminalFontSettings()
        )
        if not isinstance(self._terminal_font_settings, TerminalFontSettings):
            raise TypeError("terminal_font must be TerminalFontSettings or None")
        self._terminal_scrollback_lines = _validated_terminal_scrollback_lines(
            terminal_scrollback_lines
        )
        if type(visual_bell_enabled) is not bool:
            raise TypeError("visual_bell_enabled must be a bool")
        self._visual_bell_enabled = visual_bell_enabled
        self._terminal_highlighting = TerminalHighlighting()
        self._attention_pages: set[TerminalPage] = set()
        application = QApplication.instance()
        self._application_active = bool(
            isinstance(application, QApplication)
            and application.applicationState() == Qt.ApplicationState.ApplicationActive
        )
        self._window_active = False
        self._next_local_terminal_number = 1
        self._local_terminal_settings = LocalTerminalSettings()
        self._backspace_sends_ctrl_h = False
        self._published_terminal_count = 0
        self._closing_pages: set[TerminalPage | RdpPage] = set()
        self._command_contexts: dict[TerminalPage, object] = {}
        self._local_reconnect_pages: set[TerminalPage] = set()
        self._connection_reconnect_profiles: dict[TerminalPage, ConnectionProfile] = {}
        self._ssh_login_prompt_contexts: dict[InlineTerminalPromptPage, object] = {}
        self._shutdown_all_requested = False
        self._shutdown_all_in_progress = False
        self._tab_title_dialog: TerminalTabTitleDialog | None = None
        self._tab_title_dialog_session_id: UUID | None = None
        self._tab_context_menu: QMenu | None = None

        self._shortcut_event_filter = _TerminalShortcutEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._pane_ids: dict[QTabWidget, UUID] = {}
        self._pane_layout_timer = QTimer(self)
        self._pane_layout_timer.setSingleShot(True)
        self._pane_layout_timer.timeout.connect(self._synchronize_pane_grids)
        self.panes = TerminalPaneManager(self)
        self.panes.tab_move_guard = lambda: self.accepts_new_terminals
        self._configure_tab_group(self.tabs)
        self.panes.pane_created.connect(self._configure_tab_group)
        self.panes.active_pane_changed.connect(self._handle_active_pane_changed)
        self.panes.layout_changed.connect(self._handle_pane_layout_changed)
        self.panes.maximized_changed.connect(self._handle_pane_maximized_changed)
        self.panes.open_session_requested.connect(self.new_connection_requested)
        self.panes.tab_dropped.connect(self._focus_active_pane)
        self.panes.tab_drag_cancelled.connect(self._focus_active_pane)
        if isinstance(application, QApplication):
            application.applicationStateChanged.connect(self._handle_application_state_changed)
            application.focusWindowChanged.connect(self._handle_focus_window_changed)

        self._start_page = self._create_start_page()
        self._maximized_pane_banner = self._create_maximized_pane_banner()
        layout.addWidget(self._maximized_pane_banner)
        self._show_start_page()
        layout.addWidget(self.panes)

    def _create_maximized_pane_banner(self) -> QFrame:
        """Keep a discoverable exit from the temporary single-pane view."""
        banner = QFrame(self)
        banner.setObjectName("maximizedPaneBanner")
        banner.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        row = QHBoxLayout(banner)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(12)
        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(2)
        self._maximized_pane_title = QLabel(tr("Pane expanded"), banner)
        self._maximized_pane_title.setObjectName("maximizedPaneTitle")
        self._maximized_pane_title.setTextFormat(Qt.TextFormat.PlainText)
        self._maximized_pane_title.setWordWrap(True)
        text_column.addWidget(self._maximized_pane_title)
        hint = QLabel(tr("Other panes stay open in the background."), banner)
        hint.setObjectName("maximizedPaneHint")
        hint.setWordWrap(True)
        text_column.addWidget(hint)
        row.addLayout(text_column, 1)
        restore_button = QPushButton(tr("Restore layout"), banner)
        restore_button.setObjectName("restorePaneLayoutButton")
        restore_button.setProperty("actionRole", "secondary")
        restore_button.setAccessibleName(tr("Restore all terminal panes"))
        restore_button.clicked.connect(self.restore_pane_layout_view)
        row.addWidget(restore_button)
        banner.hide()
        return banner

    @property
    def tabs(self) -> QTabWidget:
        """Expose the active native tab group for pane-local navigation."""
        return self.panes.active_tabs

    @property
    def pane_count(self) -> int:
        return self.panes.pane_count

    @property
    def pane_is_maximized(self) -> bool:
        return self.panes.is_maximized

    def toggle_maximize_pane(self, _checked: bool = False) -> bool:
        """Expand/restore the active pane without opening or moving a session."""
        if not self.accepts_new_terminals or self.pane_count < 2:
            return False
        changed = self.panes.toggle_maximize_active()
        if changed:
            self._focus_active_pane()
        return changed

    def restore_pane_layout_view(self, _checked: bool = False) -> bool:
        """Leave the temporary expanded view and restore its input focus."""
        restored = self.panes.restore_maximized()
        if restored:
            self._focus_active_pane()
        return restored

    def _handle_pane_maximized_changed(self, maximized: bool) -> None:
        # Banner height is presentation chrome, not an intermediate PTY size.
        self._pane_layout_timer.start(0)
        if maximized:
            number = self.panes.tab_groups.index(self.tabs) + 1
            self._maximized_pane_title.setText(tr("Pane {p0} expanded", p0=f"{number}"))
        self._maximized_pane_banner.setVisible(maximized)

    @property
    def can_split_pane(self) -> bool:
        """Allow editing restored empty layouts without creating a connection."""
        return (
            self.accepts_new_terminals
            and self.pane_count < self.panes.MAX_PANES
            and (self.terminal_count > 0 or self.pane_count > 1)
        )

    def capture_pane_layout(self) -> TerminalPaneLayout:
        """Capture geometry only, never terminal or connection state."""
        return self.panes.capture_layout()

    def apply_pane_layout(self, layout: TerminalPaneLayout) -> bool:
        """Apply an explicit whole-workspace layout without restarting sessions."""
        if type(layout) is not TerminalPaneLayout:
            raise TypeError("layout must be TerminalPaneLayout")
        layout.__post_init__()
        if not self.accepts_new_terminals:
            return False
        self._hide_start_page()
        applied = self.panes.apply_layout(layout)
        if not self.terminal_count:
            self._show_empty_workspace()
        self._focus_active_pane()
        return applied

    def restore_pane_layout(self, layout: TerminalPaneLayout) -> bool:
        """Restore empty startup panes without disturbing any existing session."""
        if not isinstance(layout, TerminalPaneLayout):
            raise TypeError("layout must be TerminalPaneLayout")
        if not self.accepts_new_terminals or self.terminal_count:
            return False
        self._hide_start_page()
        restored = self.panes.restore_layout(layout)
        self._show_empty_workspace()
        return restored

    @property
    def current_pane_id(self) -> UUID:
        return self._pane_ids[self.tabs]

    def pane_id_for_session(self, session_id: UUID) -> UUID | None:
        page = self._page_for_session(session_id)
        owner = self.panes.owner_of(page) if page is not None else None
        return self._pane_ids.get(owner)

    def pane_available(self, pane_id: UUID) -> bool:
        return any(self._pane_ids.get(tabs) == pane_id for tabs in self.panes.tab_groups)

    def _tabs_for_pane(self, pane_id: UUID | None) -> QTabWidget:
        if pane_id is None:
            return self.tabs
        if not isinstance(pane_id, UUID):
            raise TypeError("pane_id must be a UUID or None")
        for tabs in self.panes.tab_groups:
            if self._pane_ids.get(tabs) == pane_id:
                return tabs
        raise ValueError("The destination terminal pane is no longer available")

    def _all_pages(self) -> tuple[QWidget, ...]:
        return tuple(
            tabs.widget(index) for tabs in self.panes.tab_groups for index in range(tabs.count())
        )

    def open_terminal_sessions(self) -> tuple[OpenTerminalSession, ...]:
        """Read safe metadata in visual pane/tab order, including hidden panes.

        No output, command, input value, backend, or credential context is read
        or retained. An inline username prompt is itself an open session.
        """
        sessions: list[OpenTerminalSession] = []
        for pane_number, tabs in enumerate(self.panes.tab_groups, 1):
            for index in range(tabs.count()):
                page = tabs.widget(index)
                if not isinstance(page, (TerminalPage, InlineTerminalPromptPage, RdpPage)):
                    continue
                if page in self._closing_pages:
                    continue
                if isinstance(page, InlineTerminalPromptPage):
                    title = page.terminal_name
                    state = OpenTerminalState.LOGIN
                elif isinstance(page, RdpPage):
                    title = page.display_title
                    state = OpenTerminalState.ENDED if page.session_ended else OpenTerminalState.OPEN
                else:
                    title = page.display_title
                    state = _terminal_page_state(page)
                sessions.append(
                    OpenTerminalSession(
                        session_id=page.session_id,
                        title=sanitize_terminal_title(title) or tr("Terminal"),
                        session_name=sanitize_terminal_title(page.terminal_name) or tr("Terminal"),
                        pane_number=pane_number,
                        state=state,
                        active=tabs is self.tabs and tabs.currentWidget() is page,
                        attention=page in self._attention_pages,
                    )
                )
        return tuple(sessions)

    def ssh_session_profile(self, session_id: object) -> ConnectionProfile | None:
        """Return only the immutable launch profile, never command/auth/terminal data."""
        if not isinstance(session_id, UUID) or not self.accepts_new_terminals:
            return None
        page = self._page_for_session(session_id)
        if not isinstance(page, TerminalPage) or page in self._closing_pages:
            return None
        profile = self._connection_reconnect_profiles.get(page)
        return (
            profile if profile is not None and profile.protocol is ConnectionProtocol.SSH else None
        )

    def ssh_session_status(self, session_id: object) -> SshSessionStatus | None:
        """Read only application-owned state of the exact launched SSH page."""
        if self.ssh_session_profile(session_id) is None:
            return None
        page = self._page_for_session(session_id)
        if not isinstance(page, TerminalPage):
            return None
        state = _terminal_page_state(page)
        return SshSessionStatus(state, page.exit_code if state is OpenTerminalState.ENDED else None)

    def ssh_system_config(self, session_id):
        """Non-secret immutable launch snapshot for the tab's separate file owner."""
        if self.ssh_session_profile(session_id) is None:
            return None
        page = self._page_for_session(session_id)
        return getattr(self._command_contexts.get(page),'system_config',None)

    def ssh_session_negotiation(self, session_id: object) -> SshNegotiatedFacts | None:
        """Return verified facts only from this exact live Embedded SSH owner."""
        if self.ssh_session_profile(session_id) is None:
            return None
        page = self._page_for_session(session_id)
        from termana.terminal.embedded_ssh_backend import EmbeddedSshBackend

        if (
            not isinstance(page, TerminalPage)
            or page.session_ended
            or not isinstance(page.backend, EmbeddedSshBackend)
            or not page.backend.is_running
        ):
            return None
        return page.backend.negotiated_facts

    @property
    def current_ssh_session_id(self) -> UUID | None:
        page = self.tabs.currentWidget()
        if isinstance(page, TerminalPage) and self.ssh_session_profile(page.session_id) is not None:
            return page.session_id
        return None

    def activate_terminal_session(self, session_id: UUID) -> bool:
        """Focus the exact live session without launching, moving, or writing to it."""
        if type(session_id) is not UUID or not self.accepts_new_terminals:
            return False
        page = self._page_for_session(session_id)
        if page is None or page in self._closing_pages:
            return False
        owner = self.panes.owner_of(page)
        if owner is None:
            return False
        # The pane manager restores maximization only when changing panes.
        if not self.panes.set_active_tabs(owner):
            return False
        # Pane changes emit synchronous signals: resolve ownership again before
        # selecting, so a listener that closes/moves a session cannot retarget it.
        if not self.accepts_new_terminals or self._page_for_session(session_id) is not page:
            return False
        owner = self.panes.owner_of(page)
        if owner is None or owner is not self.tabs:
            return False
        owner.setCurrentWidget(page)
        if (
            not self.accepts_new_terminals
            or self._page_for_session(session_id) is not page
            or self.panes.owner_of(page) is not self.tabs
            or self.tabs.currentWidget() is not page
        ):
            return False
        self._focus_terminal_input(page)
        return True

    def _publish_open_sessions_changed(self) -> None:
        """Publish lightweight invalidation; consumers coalesce metadata reads.

        This is never connected to terminal data and does not itself scan tabs.
        Read-only consumers inspect metadata only while their own window is open.
        """
        self.open_sessions_changed.emit()

    @property
    def active_pane_terminal_count(self) -> int:
        return sum(
            isinstance(self.tabs.widget(index), (TerminalPage, InlineTerminalPromptPage, RdpPage))
            for index in range(self.tabs.count())
        )

    def _configure_tab_group(self, tabs: QTabWidget) -> None:
        self._pane_ids[tabs] = uuid4()
        tabs.setAccessibleName(tr("Terminal sessions"))
        tabs.setDocumentMode(True)
        tabs.setMovable(True)
        tabs.setUsesScrollButtons(True)
        tabs.setTabsClosable(True)
        self._shortcut_event_filter.watch(tabs)
        tabs.tabBar().setExpanding(False)
        # Cocoa's document-mode base otherwise paints an OS-colored strip over
        # the themed background beyond the last tab (notably in light themes).
        tabs.tabBar().setDrawBase(False)
        tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        tabs.tabBar().tabMoved.connect(
            lambda _source, _target: self._publish_open_sessions_changed()
        )
        tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tabs.tabBar().customContextMenuRequested.connect(
            lambda position: self._show_tab_context_menu(position, tabs=tabs)
        )
        tabs.tabBarDoubleClicked.connect(
            lambda index: self._rename_terminal_page(tabs.widget(index))
        )
        tabs.tabCloseRequested.connect(lambda index: self._request_page_close(tabs.widget(index)))
        tabs.currentChanged.connect(lambda index: self._handle_group_current_changed(tabs, index))

    def _set_session_icon(self, tabs, index, protocol):
        page = tabs.widget(index)
        page.setProperty("sessionProtocol", protocol)
        glyph = {"ssh": "server", "telnet": "terminal", "rdp": "desktop"}.get(protocol, "terminal")
        attention = isinstance(page, TerminalPage) and (
            page in self._attention_pages or page.ssh_secret_tab_label is not None
        )
        color = self._interface_colors.accent if attention else self._interface_colors.text
        if not attention and page.property("connectionAccent"):
            color = connection_icon_color(page.property("connectionAccent"), self._interface_colors.surface,
                                          self._interface_colors.raised)
        tabs.setTabIcon(index, interface_icon(glyph, color, size=16))

    @staticmethod
    def _bind_connection_color(page, profile):
        if profile is not None:
            page.setProperty("connectionProfileId", str(profile.profile_id))
            page.setProperty("connectionAccent", profile.accent_color)

    def update_connection_colors(self, profiles) -> None:
        """Update visual metadata only; never replace a live launch/auth snapshot."""
        colors = {str(profile.profile_id): profile.accent_color for profile in profiles}
        for page in self._all_pages():
            profile_id = page.property("connectionProfileId")
            if profile_id in colors:
                page.setProperty("connectionAccent", colors[profile_id])
                tabs = self.panes.owner_of(page)
                self._set_session_icon(tabs, tabs.indexOf(page), page.property("sessionProtocol"))

    def _handle_group_current_changed(self, tabs: QTabWidget, index: int) -> None:
        if tabs is self.tabs:
            self._handle_current_tab_changed(index)

    def _handle_active_pane_changed(self, _tabs: QTabWidget) -> None:
        self._handle_current_tab_changed(self.tabs.currentIndex())
        if not self.panes.is_changing:
            page = self.tabs.currentWidget()
            focused = QApplication.focusWidget()
            if (
                page is None
                or focused is None
                or not (focused is page or page.isAncestorOf(focused))
            ):
                self._focus_active_pane()

    def _handle_pane_layout_changed(self) -> None:
        self._pane_layout_timer.start(0)
        groups = self.panes.tab_groups
        self._pane_ids = {
            tabs: identity for tabs, identity in self._pane_ids.items() if tabs in groups
        }
        self._sync_tab_bar_visibility()
        self.pane_layout_changed.emit()
        self.active_terminal_changed.emit()
        self._publish_open_sessions_changed()

    def _synchronize_pane_grids(self) -> None:
        """Reconcile fonts and geometry after Qt has repolished the moved panes."""
        for page in self._all_pages():
            if isinstance(page, (TerminalPage, InlineTerminalPromptPage)):
                # Stylesheet reparenting can restore Qt's original widget font.
                # Each page's preference, not the workspace default, owns its font.
                page.set_terminal_font(page.terminal_font_settings)
            if isinstance(page, TerminalPage) and page.isVisible():
                page.output_view.synchronize_grid_size()
            elif isinstance(page, RdpPage) and page.isVisible():
                page.synchronize_workspace_geometry()

    def _pane_grid_may_resize(self) -> bool:
        return not self.panes.is_changing and not self._pane_layout_timer.isActive()

    def split_right(self, _checked: bool = False) -> bool:
        return self._split_pane(Qt.Orientation.Horizontal)

    def split_down(self, _checked: bool = False) -> bool:
        return self._split_pane(Qt.Orientation.Vertical)

    def _split_pane(self, orientation: Qt.Orientation) -> bool:
        if not self.can_split_pane:
            return False
        if self.panes.split_active(orientation) is None:
            return False
        self._focus_active_pane()
        return True

    def _focus_active_pane(self) -> None:
        page = self.tabs.currentWidget()
        if page is not None:
            self._focus_terminal_input(page)
        else:
            # Empty panes must not leave keyboard input in the old connection.
            self.panes.focus_active_empty_pane()

    def focus_next_pane(self, _checked: bool = False) -> bool:
        groups = self.panes.tab_groups
        if len(groups) < 2:
            return False
        self.panes.set_active_tabs(groups[(groups.index(self.tabs) + 1) % len(groups)])
        self._focus_active_pane()
        return True

    def move_current_to_next_pane(self, _checked: bool = False) -> bool:
        groups = self.panes.tab_groups
        page = self.tabs.currentWidget()
        if len(groups) < 2 or not isinstance(page, (TerminalPage, InlineTerminalPromptPage, RdpPage)):
            return False
        target = groups[(groups.index(self.tabs) + 1) % len(groups)]
        moved = self.panes.move_page(page, target)
        if moved:
            self._focus_active_pane()
        return moved

    def merge_current_pane(self, _checked: bool = False) -> bool:
        merged = self.panes.merge_active()
        if merged:
            if self.terminal_count == 0:
                self._show_empty_workspace()
            self._focus_active_pane()
        return merged

    def merge_all_panes(self, _checked: bool = False) -> bool:
        merged = self.panes.merge_all()
        if merged:
            if self.terminal_count == 0:
                self._show_empty_workspace()
            self._focus_active_pane()
        return merged

    def _page_may_take_focus(self, page: QWidget) -> bool:
        return (
            not self.panes.is_changing
            and self.panes.owner_of(page) is self.tabs
            and self.tabs.currentWidget() is page
        )

    def _enable_shortcut_event_filter(self) -> None:
        """Enable the scoped terminal shortcut proxy exactly once."""
        event_filter = self._shortcut_event_filter
        if event_filter is not None and isValid(event_filter):
            event_filter.attach()

    def _disable_shortcut_event_filter(self) -> None:
        """Disable the scoped shortcut proxy while the workspace remains valid."""
        event_filter = self._shortcut_event_filter
        if event_filter is not None and isValid(event_filter):
            event_filter.detach()

    def suspend_shortcut_fallback(self) -> None:
        """Stop the terminal-owned fallback before an owning window retires.

        Closing a parent window does not deliver a close event to each child
        widget.  The owner therefore calls this hook as soon as its own close
        becomes irreversible, so a hidden workspace cannot keep observing its
        retired input controls while Qt defers destruction of the widget tree.
        """
        self._disable_shortcut_event_filter()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Disable shortcut fallback before a standalone workspace is closed."""
        self.suspend_shortcut_fallback()
        self._dismiss_tab_title_controls()
        super().closeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        """Restore shortcut fallback if a previously closed workspace is reused."""
        self._enable_shortcut_event_filter()
        super().showEvent(event)
        application = QApplication.instance()
        self._application_active = bool(
            isinstance(application, QApplication)
            and application.applicationState() == Qt.ApplicationState.ApplicationActive
        )
        self._window_active = self.window().isActiveWindow()
        page = self.tabs.currentWidget()
        if isinstance(page, TerminalPage) and self._page_is_actively_viewed(page):
            self._clear_terminal_attention(page)

    def _filter_terminal_shortcut_event(self, watched: QObject, event: QEvent) -> bool:
        """Recover reserved shortcuts that Qt did not dispatch as window actions."""
        if (
            isinstance(watched, QWidget)
            and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.ContextMenu)
            and self.isAncestorOf(watched)
        ):
            for page in self._all_pages():
                if page is watched or page.isAncestorOf(watched):
                    self.panes.set_active_tabs(self.panes.owner_of(page))
                    break
        if (
            not isinstance(event, QKeyEvent)
            or event.type() != QEvent.Type.KeyPress
            or not isinstance(watched, QWidget)
            or watched is self
            or not self.isAncestorOf(watched)
        ):
            return False

        tab_action = terminal_tab_action_for_key_event(event)
        if tab_action is not None:
            event.accept()
            if not event.isAutoRepeat():
                self._perform_terminal_tab_action(tab_action)
            return True

        zoom_action = zoom_action_for_key_event(event)
        if zoom_action is not None and not self.current_is_rdp:
            event.accept()
            if not event.isAutoRepeat():
                self.terminal_zoom_requested.emit(zoom_action)
            return True

        return False

    def _perform_terminal_tab_action(self, action: TerminalTabAction) -> None:
        """Run one recognized tab command through existing guarded workspace paths."""
        if action is TerminalTabAction.NEW_LOCAL:
            self.open_local_terminal()
        elif action is TerminalTabAction.CLOSE_CURRENT:
            self.request_close_current_terminal()
        elif action is TerminalTabAction.NEXT:
            self.activate_next_terminal()
        elif action is TerminalTabAction.PREVIOUS:
            self.activate_previous_terminal()

    @property
    def terminal_count(self) -> int:
        """Return the number of open terminal tabs."""
        return sum(
            isinstance(page, (TerminalPage, InlineTerminalPromptPage, RdpPage))
            for page in self._all_pages()
        )

    @property
    def pending_shutdown_count(self) -> int:
        """Return the number of hidden pages still stopping their backends."""
        return len(self._closing_pages)

    @property
    def active_terminal_count(self) -> int:
        """Return visible terminals whose processes would be interrupted by shutdown."""
        return sum(
            1
            for page in self._all_pages()
            if isinstance(page, (TerminalPage, RdpPage)) and page.requires_close_confirmation
        )

    @property
    def accepts_new_terminals(self) -> bool:
        """Return whether the workspace still admits new terminal sessions."""
        return not self._shutdown_all_requested

    @property
    def terminal_colors(self) -> TerminalColors:
        """Return the palette used by current and future terminal pages."""
        return self._terminal_colors

    @property
    def terminal_font_settings(self) -> TerminalFontSettings:
        """Return the font preference used by current and future pages."""
        return self._terminal_font_settings

    @property
    def terminal_scrollback_lines(self) -> int:
        """Return the history capacity used when a terminal screen is created."""
        return self._terminal_scrollback_lines

    @property
    def visual_bell_enabled(self) -> bool:
        """Return whether terminal bells may create visible page and tab feedback."""
        return self._visual_bell_enabled

    def set_interface_colors(self, colors: ThemeColors) -> None:
        """Tab icons belong to the UI theme, not a custom terminal ANSI palette."""
        if colors == self._interface_colors:
            return
        self._interface_colors = colors
        for page in self._all_pages():
            tabs = self.panes.owner_of(page)
            protocol = page.property("sessionProtocol")
            if tabs is not None and protocol:
                self._set_session_icon(tabs, tabs.indexOf(page), protocol)

    def set_terminal_colors(self, colors: TerminalColors) -> None:
        """Recolor every visible terminal and remember colors for new tabs."""
        if colors == self._terminal_colors:
            return
        self._terminal_colors = colors
        for page in self._all_pages():
            tabs = self.panes.owner_of(page)
            protocol = page.property("sessionProtocol")
            if tabs is not None and protocol:
                self._set_session_icon(tabs, tabs.indexOf(page), protocol)
            if isinstance(page, (TerminalPage, InlineTerminalPromptPage)):
                page.set_terminal_colors(colors)

    def set_terminal_font(self, settings: TerminalFontSettings) -> None:
        """Apply a font to every open page and remember it for future tabs."""
        if not isinstance(settings, TerminalFontSettings):
            raise TypeError("settings must be TerminalFontSettings")
        if settings == self._terminal_font_settings:
            return
        self._terminal_font_settings = settings
        for page in self._all_pages():
            if isinstance(page, (TerminalPage, InlineTerminalPromptPage)):
                page.set_terminal_font(settings)

    def set_terminal_scrollback_lines(self, lines: int) -> None:
        """Remember a history limit for future sessions without truncating open tabs."""
        self._terminal_scrollback_lines = _validated_terminal_scrollback_lines(lines)

    def set_highlighting(self, settings: TerminalHighlighting) -> None:
        if type(settings) is not TerminalHighlighting:
            raise TypeError("settings must be TerminalHighlighting")
        self._terminal_highlighting = settings
        for page in self._all_pages():
            if isinstance(page, TerminalPage):
                page.output_view.set_highlighting(settings)

    def set_visual_bell_enabled(self, enabled: bool) -> None:
        """Apply one live visual-bell preference to current and future terminals."""
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        if enabled == self._visual_bell_enabled:
            return
        self._visual_bell_enabled = enabled
        visible_pages = tuple(page for page in self._all_pages() if isinstance(page, TerminalPage))
        closing_terminals = tuple(page for page in self._closing_pages if isinstance(page, TerminalPage))
        for page in (*visible_pages, *closing_terminals):
            page.set_visual_bell_enabled(enabled)
        if enabled:
            return
        marked_pages = tuple(self._attention_pages)
        self._attention_pages.clear()
        for page in marked_pages:
            self._refresh_terminal_tab_label(page)

    def open_local_terminal(self, *, pane_id: UUID | None = None) -> str | None:
        """Open a tab backed by a new local shell process."""
        if not self.accepts_new_terminals:
            return None
        self._tabs_for_pane(pane_id)
        terminal_name = f"{tr(LOCAL_TAB_TITLE_PREFIX)} {self._next_local_terminal_number}"
        try:
            command = local_shell_command(self._local_terminal_settings)
        except (OSError, ValueError) as error:
            self.local_terminal_error.emit(tr(str(error)))
            return None
        backend = self._backend_factory(command)
        self._open_terminal(
            terminal_name, backend, local_reconnect=True, pane_id=pane_id,
            local_start_directory=self._local_terminal_settings.directory,
        )
        self._next_local_terminal_number += 1
        self.local_terminal_opened.emit(terminal_name)
        return terminal_name

    @property
    def current_is_rdp(self) -> bool:
        return isinstance(self.tabs.currentWidget(), RdpPage)

    def open_rdp_session(self, host: str, port: int = 3389, *, display_size=(1280, 720),
                         page_factory=RdpPage, profile=None):
        """Insert one graphical page; no terminal backend or profile/store coercion."""
        if not self.accepts_new_terminals:
            return None
        page = (page_factory(host, port) if display_size == (1280, 720)
                else page_factory(host, port, display_size=display_size))
        if profile is not None:
            page.apply_profile(profile)
        self._bind_connection_color(page, profile)
        page.set_focus_guard(lambda: self._page_may_take_focus(page))
        def duplicate():
            if self._page_may_take_focus(page) and page.isVisible():
                self.open_rdp_session(host, port, display_size=page.controller.display_size,
                                      page_factory=page_factory)
        page.duplicate_requested.connect(duplicate)
        tabs = self.tabs
        self._hide_start_page()
        index = tabs.addTab(page, _qt_tab_label(page.display_title))
        self._set_session_icon(tabs, index, "rdp")
        tabs.setTabToolTip(index, plain_text_tooltip(page.display_title))
        tabs.setTabsClosable(True)
        tabs.setCurrentWidget(page)
        self._shortcut_event_filter.watch(page.surface)
        page.state_changed.connect(self._publish_open_sessions_changed)
        self._sync_tab_bar_visibility()
        self._publish_terminal_count()
        page.start()
        return page

    def set_local_terminal_settings(self, settings: LocalTerminalSettings) -> None:
        """Apply a launch default without rerouting or restarting any live page."""
        if type(settings) is not LocalTerminalSettings:
            raise TypeError("Expected LocalTerminalSettings")
        self._local_terminal_settings = settings

    def set_backspace_sends_ctrl_h(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("Backspace preference must be boolean")
        self._backspace_sends_ctrl_h = enabled
        for page in self._all_pages():
            if isinstance(page, TerminalPage):
                page.output_view.set_backspace_sends_ctrl_h(enabled)

    def open_ssh_login_prompt_session(
        self,
        terminal_name: str,
        context: object,
        *,
        pane_id: UUID | None = None,
        profile: ConnectionProfile | None = None,
    ) -> UUID | None:
        """Open an application-owned inline username prompt without a backend."""
        if not self.accepts_new_terminals:
            return None
        tabs = self._tabs_for_pane(pane_id)
        page = InlineTerminalPromptPage(
            terminal_name,
            terminal_colors=self._terminal_colors,
            terminal_font=self._terminal_font_settings,
        )
        page.set_focus_guard(lambda: self._page_may_take_focus(page))
        self._shortcut_event_filter.watch(page.username_input)
        self._ssh_login_prompt_contexts[page] = context
        page.submitted.connect(self._forward_ssh_login_submitted)
        page.cancel_requested.connect(self._cancel_ssh_login_prompt)
        self._hide_start_page()
        tab_index = tabs.addTab(page, _qt_tab_label(terminal_name))
        self._bind_connection_color(page, profile)
        self._set_session_icon(tabs, tab_index, "ssh")
        tabs.setTabToolTip(tab_index, plain_text_tooltip(terminal_name))
        self._sync_tab_bar_visibility()
        tabs.setTabsClosable(True)
        tabs.setCurrentIndex(tab_index)
        self._publish_terminal_count()
        self._focus_new_session(page)
        return page.session_id

    def promote_ssh_login_prompt_session(
        self,
        session_id: UUID,
        command: ShellCommand,
        command_context: object,
        terminal_name: str,
        *,
        requires_direct_input: bool = True,
        reconnect_profile: ConnectionProfile | None = None,
    ) -> UUID | None:
        """Replace one submitted login prompt with its command using the same UUID."""
        prompt_page = self._page_for_session(session_id)
        if not isinstance(prompt_page, InlineTerminalPromptPage):
            return None
        if not self.accepts_new_terminals or prompt_page.submitted_username is None:
            prompt_page.reset_submission()
            return None
        if reconnect_profile is not None and not isinstance(
            reconnect_profile,
            ConnectionProfile,
        ):
            prompt_page.reset_submission()
            raise TypeError("reconnect_profile must be a ConnectionProfile or None")

        try:
            backend = self._backend_factory(command)
        except Exception:
            prompt_page.reset_submission()
            raise
        if not isinstance(backend, TerminalBackend):
            prompt_page.reset_submission()
            raise TypeError("backend factory must return a TerminalBackend")
        return self.promote_ssh_login_prompt_backend(
            session_id,
            backend,
            command_context,
            terminal_name,
            requires_direct_input=requires_direct_input,
            reconnect_profile=reconnect_profile,
        )

    def promote_ssh_login_prompt_backend(
        self,
        session_id: UUID,
        backend: TerminalBackend,
        command_context: object,
        terminal_name: str,
        *,
        requires_direct_input: bool = True,
        reconnect_profile: ConnectionProfile | None = None,
    ) -> UUID | None:
        """Adopt a cold backend into a submitted prompt, preserving pane and UUID."""
        if not isinstance(backend, TerminalBackend):
            raise TypeError("backend must be a TerminalBackend")
        prompt_page = self._page_for_session(session_id)
        if not isinstance(prompt_page, InlineTerminalPromptPage):
            backend.close()
            backend.deleteLater()
            return None
        if not self.accepts_new_terminals or prompt_page.submitted_username is None:
            backend.close()
            backend.deleteLater()
            prompt_page.reset_submission()
            return None
        if reconnect_profile is not None and not isinstance(reconnect_profile, ConnectionProfile):
            backend.close()
            backend.deleteLater()
            prompt_page.reset_submission()
            raise TypeError("reconnect_profile must be a ConnectionProfile or None")
        if requires_direct_input and not backend.supports_direct_input:
            backend.close()
            backend.deleteLater()
            prompt_page.reset_submission()
            return None

        try:
            terminal_page = self._create_terminal_page(
                terminal_name,
                backend,
                session_id=session_id,
                reconnect_available=reconnect_profile is not None,
            )
        except Exception:
            backend.deleteLater()
            prompt_page.reset_submission()
            raise
        terminal_page.display_title_changed.connect(self._update_terminal_title)
        terminal_page.configure_session_title(ssh=True)
        terminal_page.output_view.set_word_delete_sends_ctrl_w(
            reconnect_profile is not None and reconnect_profile.word_delete_mode is WordDeleteMode.CTRL_W
        )
        terminal_page.ssh_secret_submitted.connect(self._forward_ssh_secret_submitted)
        terminal_page.ssh_secret_cancel_requested.connect(self._forward_ssh_secret_cancel_requested)
        self._command_contexts[terminal_page] = command_context
        if reconnect_profile is not None:
            self._connection_reconnect_profiles[terminal_page] = reconnect_profile
        terminal_page.process_started.connect(self._forward_command_terminal_started)
        terminal_page.process_finished.connect(self._forward_command_terminal_finished)
        terminal_page.reconnect_requested.connect(self._handle_terminal_reconnect_requested)
        terminal_page.ended_close_requested.connect(self._handle_ended_terminal_close_requested)

        self.panes.replace_page(prompt_page, terminal_page, _qt_tab_label(terminal_name))
        owner_tabs = self.panes.owner_of(terminal_page)
        self._bind_connection_color(terminal_page, reconnect_profile)
        # Preserve an edit made while this exact login prompt was open.
        if prompt_page.property("connectionProfileId") == terminal_page.property("connectionProfileId"):
            terminal_page.setProperty("connectionAccent", prompt_page.property("connectionAccent"))
        self._set_session_icon(owner_tabs, owner_tabs.indexOf(terminal_page), "ssh")
        self._refresh_terminal_tab_label(terminal_page)

        try:
            # The validated login is public terminal text, not a credential.
            # Insert it before the transport can emit its first auth question;
            # never put a password/answer into this initial transcript.
            login_line = tr("login as: {p0}\r\n", p0=f"{prompt_page.submitted_username}").encode(
                backend.text_encoding, errors="replace"
            )
            terminal_page.start(initial_output=login_line)
        except Exception:
            self._command_contexts.pop(terminal_page, None)
            self._connection_reconnect_profiles.pop(terminal_page, None)
            self.panes.replace_page(
                terminal_page, prompt_page, _qt_tab_label(prompt_page.terminal_name)
            )
            prompt_page.reset_submission()
            self._begin_page_shutdown(terminal_page)
            raise

        self._ssh_login_prompt_contexts.pop(prompt_page, None)
        prompt_page.deleteLater()
        self._focus_new_session(terminal_page)
        return session_id

    def ssh_login_prompt_submission(self, session_id: UUID) -> tuple[object, str] | None:
        """Snapshot only a still-owned submitted prompt, never an ended command tab."""
        page = self._page_for_session(session_id)
        if (
            not isinstance(page, InlineTerminalPromptPage)
            or page not in self._ssh_login_prompt_contexts
            or page.submitted_username is None
        ):
            return None
        return self._ssh_login_prompt_contexts[page], page.submitted_username

    def show_ssh_login_prompt_error(self, session_id: UUID, message: str) -> bool:
        """Show one recoverable promotion error and re-enable its inline prompt."""
        page = self._page_for_session(session_id)
        if not isinstance(page, InlineTerminalPromptPage):
            return False
        page.reset_submission()
        page.show_error(message)
        return True

    def open_command_terminal(
        self,
        terminal_name: str,
        command: ShellCommand,
        *,
        requires_direct_input: bool = False,
        session_context: object | None = None,
        reconnect_profile: ConnectionProfile | None = None,
        pane_id: UUID | None = None,
    ) -> bool:
        """Open one command session when the selected backend can support it."""
        return (
            self.open_command_terminal_session(
                terminal_name,
                command,
                requires_direct_input=requires_direct_input,
                session_context=session_context,
                reconnect_profile=reconnect_profile,
                pane_id=pane_id,
            )
            is not None
        )

    def open_command_terminal_session(
        self,
        terminal_name: str,
        command: ShellCommand,
        *,
        requires_direct_input: bool = False,
        session_context: object | None = None,
        reconnect_profile: ConnectionProfile | None = None,
        pane_id: UUID | None = None,
    ) -> UUID | None:
        """Open a command session and return its stable identity when accepted."""
        if not self.accepts_new_terminals:
            return None
        self._tabs_for_pane(pane_id)
        backend = self._backend_factory(command)
        return self.open_backend_terminal_session(
            terminal_name,
            backend,
            requires_direct_input=requires_direct_input,
            session_context=session_context,
            reconnect_profile=reconnect_profile,
            pane_id=pane_id,
        )

    def open_backend_terminal(
        self,
        terminal_name: str,
        backend: TerminalBackend,
        *,
        requires_direct_input: bool = True,
        session_context: object | None = None,
        reconnect_profile: ConnectionProfile | None = None,
        pane_id: UUID | None = None,
    ) -> bool:
        """Adopt and open one prepared backend, or dispose of it when unsupported."""
        return (
            self.open_backend_terminal_session(
                terminal_name,
                backend,
                requires_direct_input=requires_direct_input,
                session_context=session_context,
                reconnect_profile=reconnect_profile,
                pane_id=pane_id,
            )
            is not None
        )

    def open_backend_terminal_session(
        self,
        terminal_name: str,
        backend: TerminalBackend,
        *,
        requires_direct_input: bool = True,
        session_context: object | None = None,
        reconnect_profile: ConnectionProfile | None = None,
        pane_id: UUID | None = None,
    ) -> UUID | None:
        """Adopt a prepared backend and return the new terminal UUID when accepted."""
        if not isinstance(backend, TerminalBackend):
            raise TypeError("backend must be a TerminalBackend")
        if reconnect_profile is not None and not isinstance(
            reconnect_profile,
            ConnectionProfile,
        ):
            backend.deleteLater()
            raise TypeError("reconnect_profile must be a ConnectionProfile or None")
        if not self.accepts_new_terminals:
            backend.deleteLater()
            return None
        if requires_direct_input and not backend.supports_direct_input:
            backend.deleteLater()
            return None

        page = self._open_terminal(
            terminal_name,
            backend,
            session_context=session_context,
            reconnect_profile=reconnect_profile,
            pane_id=pane_id,
        )
        return page.session_id

    def begin_host_key_recovery(self, session_id: UUID) -> bool:
        """Reserve one ended owner without changing its pane, history or UUID."""
        page = self._page_for_session(session_id)
        return isinstance(page, TerminalPage) and page.begin_host_key_recovery()

    def replace_ended_command_terminal_session(
        self, session_id: UUID, command: ShellCommand, session_context: object
    ) -> UUID | None:
        """Continue a reviewed host-key attempt in its original ended terminal."""
        page = self._page_for_session(session_id)
        if (
            not self.accepts_new_terminals
            or not isinstance(page, TerminalPage)
            or not page.session_ended
            or not page.reconnect_request_pending
        ):
            return None
        backend = self._backend_factory(command)
        if not isinstance(backend, TerminalBackend):
            raise TypeError("backend factory must return a TerminalBackend")
        if not self.accepts_new_terminals or self._page_for_session(session_id) is not page:
            backend.close()
            backend.deleteLater()
            return None
        previous_context = self._command_contexts.get(page)
        self._command_contexts[page] = session_context
        try:
            adopted = page.replace_ended_backend(backend)
        except Exception:
            # A failed adopted transport keeps its new context for its finish
            # signal; an unadopted factory result must not replace the old owner.
            if page.backend is not backend:
                self._command_contexts[page] = previous_context
                backend.close()
                backend.deleteLater()
            raise
        if not adopted:
            self._command_contexts[page] = previous_context
            backend.close()
            backend.deleteLater()
            return None
        self._publish_terminal_count()
        return session_id

    def finish_terminal_reconnect(self, session_id: UUID, *, succeeded: bool) -> bool:
        """Resolve a pending remote reconnect against the exact visible source page."""
        if not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID")
        if not isinstance(succeeded, bool):
            raise TypeError("succeeded must be a boolean")
        page = self._page_for_session(session_id)
        if not isinstance(page, TerminalPage) or not page.finish_reconnect_request(
            succeeded=succeeded
        ):
            return False
        if succeeded:
            self._close_page(page)
        else:
            self._publish_open_sessions_changed()
        return True

    def contains_terminal_session(self, session_id: UUID) -> bool:
        """Return whether a terminal with this stable identity is still visible."""
        return self._page_for_session(session_id) is not None

    def terminal_output_export_available(self, session_id: UUID) -> bool:
        """Recheck the exact tab after any modal export step."""
        page = self._page_for_session(session_id)
        return isinstance(page, TerminalPage) and page.output_export_available

    def current_command_target(self) -> tuple[UUID, str] | None:
        """Bind the library to the terminal active when the user opens it."""
        page = self.tabs.currentWidget()
        if isinstance(page, TerminalPage) and page.command_insertion_available:
            return page.session_id, page.display_title
        return None

    @property
    def current_terminal_search_available(self) -> bool:
        """Only a live, owned terminal page has an output search surface."""
        page = self.tabs.currentWidget()
        return (
            self.accepts_new_terminals
            and isinstance(page, TerminalPage)
            and page not in self._closing_pages
        )

    def open_current_terminal_search(self, _checked: bool = False) -> bool:
        """Reuse the terminal's search signal and its secret-focus/shutdown guards."""
        if not self.current_terminal_search_available:
            return False
        self.tabs.currentWidget().output_view.find_requested.emit()
        return True

    def command_insertion_available(self, session_id: UUID) -> bool:
        target = self.current_command_target()
        return target is not None and target[0] == session_id

    def insert_saved_command(self, session_id: UUID, command: str) -> bool:
        if not self.command_insertion_available(session_id):
            return False
        return self.tabs.currentWidget().insert_saved_command(command)

    def focus_command_target(self, session_id: UUID) -> None:
        if self.command_insertion_available(session_id):
            self._focus_terminal_input(self.tabs.currentWidget())

    @Slot()
    def _forward_saved_commands_requested(self) -> None:
        page = self.sender()
        if isinstance(page, TerminalPage) and self.command_insertion_available(page.session_id):
            self.saved_commands_requested.emit(page.session_id)

    def capture_terminal_output(self, session_id: UUID) -> TerminalTextSnapshot | None:
        """Capture only the exact visible terminal, not an inline login page."""
        page = self._page_for_session(session_id)
        return page.capture_output_snapshot() if isinstance(page, TerminalPage) else None

    def terminal_session_generation(self, session_id: object) -> object | None:
        """Opaque exact current backend identity; no calls, reads or logging on it."""
        if type(session_id) is not UUID or not self.accepts_new_terminals:
            return None
        page = self._page_for_session(session_id)
        if not isinstance(page, TerminalPage) or page in self._closing_pages:
            return None
        return page.backend

    def capture_terminal_search_sources(
        self, session_ids: tuple[UUID, ...]
    ) -> TerminalSearchCapture:
        """Capture only explicitly reviewed UUIDs within aggregate row/cell budgets."""
        if (
            type(session_ids) is not tuple
            or not 1 <= len(session_ids) <= MAX_MULTI_SEARCH_SESSIONS
            or any(type(item) is not UUID for item in session_ids)
            or len(set(session_ids)) != len(session_ids)
        ):
            raise ValueError("Select 1-16 distinct terminal sessions")
        if not self.accepts_new_terminals:
            return TerminalSearchCapture(skipped=len(session_ids))
        sessions = {item.session_id: item for item in self.open_terminal_sessions()}
        sources = []
        rows = cells = skipped = budget_skipped = 0
        for session_id in session_ids:
            page = self._page_for_session(session_id)
            if (
                not isinstance(page, TerminalPage)
                or session_id not in sessions
                or page in self._closing_pages
                or not page.output_export_available
                or page.reconnect_request_pending
            ):
                skipped += 1
                continue
            count = page.screen.document_rows
            size = count * page.screen.columns
            if rows + count > MAX_MULTI_SEARCH_ROWS or cells + size > MAX_MULTI_SEARCH_CELLS:
                budget_skipped += 1
                continue
            snapshot = page.capture_output_snapshot()
            if snapshot is None:
                skipped += 1
                continue
            sources.append(
                TerminalSearchSource(
                    TerminalSearchDocument(sessions[session_id], snapshot), page.backend
                )
            )
            rows += count
            cells += size
        return TerminalSearchCapture(tuple(sources), skipped, budget_skipped)

    @Slot()
    def _forward_output_export_requested(self) -> None:
        page = self.sender()
        if isinstance(page, TerminalPage) and self.panes.owner_of(page) is not None:
            if page.output_export_available:
                self.output_export_requested.emit(page.session_id)

    def terminal_reconnect_pending(self, session_id: UUID) -> bool:
        """Return whether the exact visible ended tab still owns a reconnect request."""
        if not isinstance(session_id, UUID):
            return False
        page = self._page_for_session(session_id)
        return isinstance(page, TerminalPage) and page.reconnect_request_pending

    def close_terminal_session(self, session_id: UUID) -> bool:
        """Close a visible terminal by UUID without retaining its Qt page object."""
        page = self._page_for_session(session_id)
        if page is None:
            return False
        self._close_page(page)
        return True

    def close_ended_terminal_sessions(self, session_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        """Close only reviewed, still-ended identities; never interrupt a backend.

        A confirmation may outlive tab moves, reconnect, or application shutdown.
        Validate the entire request before mutation, then recheck each live owner.
        """
        if type(session_ids) is not tuple or any(type(item) is not UUID for item in session_ids):
            raise TypeError("Ended terminal closure requires a tuple of UUIDs")
        closed = []
        for session_id in dict.fromkeys(session_ids):
            if not self.accepts_new_terminals:
                break
            page = self._page_for_session(session_id)
            if isinstance(page, RdpPage):
                if page not in self._closing_pages and page.session_ended:
                    self._close_page(page)
                    closed.append(session_id)
                continue
            if (
                not isinstance(page, TerminalPage)
                or page in self._closing_pages
                or not page.session_ended
                or page.backend.is_running
                or page.reconnect_request_pending
                or page.ssh_secret_tab_label is not None
            ):
                continue
            self._close_page(page)
            closed.append(session_id)
        return tuple(closed)

    def request_close_current_terminal(self, _checked: bool = False) -> bool:
        """Route a keyboard/menu close through the same guarded user-close flow."""
        tab_index = self.tabs.currentIndex()
        if tab_index < 0 or self.tabs.widget(tab_index) is self._start_page:
            return False
        page = self.tabs.widget(tab_index)
        self._request_terminal_close(tab_index)
        return self.panes.owner_of(page) is None

    def activate_next_terminal(self, _checked: bool = False) -> bool:
        """Select the next terminal tab with wraparound when one exists."""
        return self._activate_adjacent_terminal(1)

    def activate_previous_terminal(self, _checked: bool = False) -> bool:
        """Select the previous terminal tab with wraparound when one exists."""
        return self._activate_adjacent_terminal(-1)

    def _activate_adjacent_terminal(self, offset: int) -> bool:
        """Move by one visible terminal tab without treating Start as a session."""
        if offset not in {-1, 1}:
            raise ValueError("Terminal tab offset must be -1 or 1")
        if self.active_pane_terminal_count < 2:
            return False
        current_index = self.tabs.currentIndex()
        if current_index < 0:
            return False
        target_index = (current_index + offset) % self.tabs.count()
        self.tabs.setCurrentIndex(target_index)
        if target_index == current_index:
            return False
        self._focus_terminal_input(self.tabs.widget(target_index))
        return True

    @staticmethod
    def _focus_terminal_input(page: QWidget | None) -> None:
        """Restore the input surface owned by a tab selected from a shortcut."""
        reason = Qt.FocusReason.ShortcutFocusReason
        if isinstance(page, RdpPage):
            if page.stack.currentWidget() is page.desktop_view:
                page.surface.setFocus(reason)
            return
        if isinstance(page, InlineTerminalPromptPage):
            if page.username_input.isEnabled():
                page.username_input.setFocus(reason)
            return
        if not isinstance(page, TerminalPage):
            return
        if (
            page.active_ssh_secret_request_id is not None
            and page.ssh_secret_prompt.secret_input.isEnabled()
        ):
            page.ssh_secret_prompt.secret_input.setFocus(reason)
        elif not page.terminal_search_bar.isHidden():
            page.terminal_search_bar.query_input.setFocus(reason)
        elif page.command_input is not None and page.command_input.isEnabled():
            page.command_input.setFocus(reason)
        else:
            page.output_view.setFocus(reason)

    def _focus_new_session(self, page: QWidget) -> None:
        """Hand an explicit open to its input, without later stealing another field."""
        original_focus = QApplication.focusWidget()

        def focus_if_owned():
            if not isValid(page) or not self.accepts_new_terminals:
                return
            focus = QApplication.focusWidget()
            if (
                self._page_may_take_focus(page)
                and page.isVisible()
                and QApplication.activeModalWidget() is None
                and (
                    focus is None
                    or focus is original_focus
                    or focus is page
                    or page.isAncestorOf(focus)
                )
            ):
                self._focus_terminal_input(page)

                if isinstance(page, TerminalPage):
                    page.acknowledge_initial_input_focus()

        focus_if_owned()
        QTimer.singleShot(0, self, focus_if_owned)

    def append_terminal_diagnostic(self, session_id: UUID, data: bytes) -> bool:
        """Append a local transport diagnostic to one still-visible terminal."""
        page = self._page_for_session(session_id)
        if not isinstance(page, TerminalPage):
            return False
        page.append_application_diagnostic(data)
        return True

    def show_connection_guidance(self, session_id: UUID, message: str) -> bool:
        """Present advisory text only in its exact still-owned terminal tab."""
        page = self._page_for_session(session_id)
        if not isinstance(page, TerminalPage):
            return False
        page.connection_advisory.show_message(message)
        return True

    def begin_ssh_secret_prompt(
        self,
        session_id: UUID,
        request_id: UUID,
        prompt: str,
        *,
        is_host_password: bool = True,
        is_host_confirmation: bool = False,
    ) -> bool:
        """Show one exact AskPass request inside its visible terminal tab."""
        page = self._page_for_session(session_id)
        if not isinstance(page, TerminalPage):
            return False
        if not page.begin_ssh_secret_prompt(
            request_id,
            prompt,
            is_host_password=is_host_password,
            is_host_confirmation=is_host_confirmation,
        ):
            return False
        self._refresh_terminal_tab_label(page)
        return True

    def finish_ssh_secret_prompt(self, session_id: UUID, request_id: UUID) -> bool:
        """Clear only the matching inline AskPass request from a visible tab."""
        page = self._page_for_session(session_id)
        if not isinstance(page, TerminalPage):
            return False
        focused = QApplication.focusWidget()
        restore_focus = (
            page is self.tabs.currentWidget()
            and focused is not None
            and (focused is page or page.isAncestorOf(focused))
        )
        # Hiding the focused password field can make Qt traverse to another
        # pane before regular input is enabled. Keep that traversal local.
        with self.panes.selection_transaction():
            if not page.finish_ssh_secret_prompt(request_id):
                return False
            self._refresh_terminal_tab_label(page)
        if restore_focus:
            self._focus_terminal_input(page)
        return True

    def close_terminal(self, tab_index: int) -> None:
        """Close one terminal tab without presenting user confirmation."""
        self._close_page(self.tabs.widget(tab_index))

    def _close_page(self, page: QWidget | None) -> None:
        """Resolve ownership at the point of mutation, never reuse a pane-local index."""
        if page is None or page is self._start_page:
            return
        tabs = self.panes.owner_of(page)
        if tabs is None:
            return
        tab_index = tabs.indexOf(page)

        if (
            isinstance(page, TerminalPage)
            and page.session_id == self._tab_title_dialog_session_id
            and self._tab_title_dialog is not None
        ):
            self._tab_title_dialog.reject()

        terminal_name = (
            page.terminal_name
            if isinstance(page, (TerminalPage, InlineTerminalPromptPage))
            else tabs.tabText(tab_index)
        )
        tabs.removeTab(tab_index)

        if self.terminal_count == 0:
            self._show_empty_workspace()
        self._publish_terminal_count()
        self.terminal_closed.emit(terminal_name)

        if isinstance(page, (TerminalPage, RdpPage)):
            self._begin_page_shutdown(page)
        elif isinstance(page, InlineTerminalPromptPage):
            self._dispose_ssh_login_prompt(page)
        else:
            page.deleteLater()

    @Slot(int)
    def _request_terminal_close(self, tab_index: int) -> None:
        """Handle a tab close control, confirming only interruption of a live terminal."""
        self._request_page_close(self.tabs.widget(tab_index))

    def _request_page_close(self, page: QWidget | None) -> None:
        if page is None or page is self._start_page or self.panes.owner_of(page) is None:
            return

        if (
            self._confirm_session_close
            and isinstance(page, (TerminalPage, RdpPage))
            and page.requires_close_confirmation
        ):
            if not self._confirm_terminal_close(page):
                return
            # The dialog runs a nested event loop, so tabs may have moved or the
            # terminal may already have been closed while it was visible.
        self._close_page(page)

    def set_confirm_session_close(self, enabled: bool) -> None:
        """Change only normal per-session close consent, never application exit."""
        if type(enabled) is not bool:
            raise TypeError("confirm_session_close must be a boolean")
        self._confirm_session_close = enabled

    def _confirm_terminal_close(self, page: TerminalPage | RdpPage) -> bool:
        """Confirm the explicitly requested terminal close; Enter accepts, Escape cancels."""
        message_box = QMessageBox(self)
        message_box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        message_box.setObjectName("closeActiveTerminalDialog")
        message_box.setWindowTitle(tr("Disconnect remote desktop?") if isinstance(page, RdpPage)
                                   else tr("Close terminal?"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(tr('Close "{p0}"?', p0=f"{page.terminal_name}"))
        message_box.setInformativeText(
            tr("Disconnect this remote desktop? Windows will not be signed out.")
            if isinstance(page, RdpPage) else
            tr("This terminal is still active. Closing it will stop the running session.")
        )

        close_button = QPushButton(tr("Disconnect") if isinstance(page, RdpPage)
                                   else tr("Close terminal"), message_box)
        close_button.setObjectName("confirmCloseTerminalButton")
        message_box.addButton(close_button, QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = QPushButton(tr("Cancel"), message_box)
        cancel_button.setObjectName("cancelCloseTerminalButton")
        message_box.addButton(cancel_button, QMessageBox.ButtonRole.RejectRole)
        message_box.setDefaultButton(close_button)
        message_box.setEscapeButton(cancel_button)

        try:
            message_box.exec()
            return message_box.clickedButton() is close_button
        finally:
            schedule_delete_later(message_box)

    def shutdown_all(self) -> bool:
        """Begin stopping every backend and report whether all are already done."""
        self._shutdown_all_requested = True
        self._dismiss_tab_title_controls()
        self._shutdown_all_in_progress = True
        pages = [
            page
            for page in self._all_pages()
            if isinstance(page, (TerminalPage, InlineTerminalPromptPage, RdpPage))
        ]

        for page in pages:
            tabs = self.panes.owner_of(page)
            if tabs is not None:
                tabs.removeTab(tabs.indexOf(page))
            if isinstance(page, (TerminalPage, RdpPage)):
                self._begin_page_shutdown(page)
            elif isinstance(page, InlineTerminalPromptPage):
                self._dispose_ssh_login_prompt(page)

        if self.terminal_count == 0:
            self._show_start_page()
        self._publish_terminal_count()
        self._shutdown_all_in_progress = False
        return not self._closing_pages

    @property
    def current_tab_can_be_renamed(self) -> bool:
        return self.accepts_new_terminals and isinstance(self.tabs.currentWidget(), TerminalPage)

    def rename_current_terminal(self, _checked: bool = False) -> None:
        """Menu entry point; do not reserve shell-owned shortcuts such as F2."""
        self.rename_terminal_tab(self.tabs.currentIndex())

    def rename_terminal_tab(self, tab_index: int) -> None:
        """Bind a display-only edit to a page identity before entering its modal loop."""
        self._rename_terminal_page(self.tabs.widget(tab_index))

    def _rename_terminal_page(self, page: QWidget | None) -> None:
        if not self.accepts_new_terminals or not isinstance(page, TerminalPage):
            return
        if self._tab_title_dialog is not None:
            self._tab_title_dialog.raise_()
            self._tab_title_dialog.activateWindow()
            return
        session_id = page.session_id
        dialog = TerminalTabTitleDialog(
            self.window(), session_name=page.terminal_name, custom_title=page.custom_tab_title
        )
        self._tab_title_dialog = dialog
        self._tab_title_dialog_session_id = session_id
        try:
            if (
                dialog.exec() != QDialog.DialogCode.Accepted
                or not self.accepts_new_terminals
                or self._tab_title_dialog_session_id != session_id
            ):
                return
            current_page = self._page_for_session(session_id)
            if isinstance(current_page, TerminalPage):
                current_page.set_custom_tab_title(dialog.tab_title)
        finally:
            self._tab_title_dialog = None
            self._tab_title_dialog_session_id = None
            schedule_delete_later(dialog)

    def _dismiss_tab_title_controls(self) -> None:
        self._tab_title_dialog_session_id = None
        menu = self._tab_context_menu
        self._tab_context_menu = None
        if menu is not None and isValid(menu):
            menu.close()
        if self._tab_title_dialog is not None and isValid(self._tab_title_dialog):
            self._tab_title_dialog.reject()

    def _show_tab_context_menu(self, position: QPoint, *, tabs: QTabWidget | None = None) -> None:
        """A moved or closed tab must never redirect a menu action to its neighbor."""
        if not self.accepts_new_terminals or self._tab_context_menu is not None:
            return
        tabs = self.tabs if tabs is None else tabs
        if tabs not in self.panes.tab_groups:
            return
        tab_bar = tabs.tabBar()
        page = tabs.widget(tab_bar.tabAt(position))
        if not isinstance(page, TerminalPage):
            return
        session_id = page.session_id
        menu = QMenu(self)
        menu.setObjectName("terminalTabContextMenu")
        rename_action = menu.addAction(tr("Rename tab…"))
        reset_action = menu.addAction(tr("Use automatic title"))
        reset_action.setEnabled(page.custom_tab_title is not None)
        lock_requested = not page.input_locked
        lock_action = menu.addAction(
            tr("Unlock terminal input") if page.input_locked else tr("Lock terminal input")
        )
        lock_action.setObjectName("terminalTabInputLockAction")
        details_action = None
        save_profile_action = None
        if self.ssh_session_profile(session_id) is not None:
            details_action = menu.addAction(tr("SSH connection details…"))
            save_profile_action = menu.addAction(tr("Save SSH session as profile…"))
        move_actions = {}
        if self.pane_count > 1:
            move_menu = menu.addMenu(tr("Move to pane"))
            for number, target in enumerate(self.panes.tab_groups, 1):
                action = move_menu.addAction(tr("Pane {p0}", p0=f"{number}"))
                action.setEnabled(target is not tabs)
                move_actions[action] = self._pane_ids[target]
        menu.addSeparator()
        close_action = menu.addAction(tr("Close tab"))
        self._tab_context_menu = menu
        try:
            selected_action = menu.exec(tab_bar.mapToGlobal(position))
            menu_still_active = self._tab_context_menu is menu
        finally:
            if self._tab_context_menu is menu:
                self._tab_context_menu = None
            schedule_delete_later(menu)
        if not menu_still_active or not self.accepts_new_terminals:
            return
        current_page = self._page_for_session(session_id)
        if current_page is None:
            return
        if selected_action is rename_action:
            self._rename_terminal_page(current_page)
        elif selected_action is reset_action and reset_action.isEnabled():
            current_page.set_custom_tab_title(None)
        elif selected_action is lock_action:
            current_page.set_input_locked(lock_requested)
        elif selected_action is close_action:
            self._request_page_close(current_page)
        elif details_action is not None and selected_action is details_action:
            self.ssh_session_details_requested.emit(session_id)
        elif save_profile_action is not None and selected_action is save_profile_action:
            self.save_ssh_session_requested.emit(session_id)
        elif selected_action in move_actions:
            pane_id = move_actions[selected_action]
            if self.pane_available(pane_id):
                self.panes.move_page(current_page, self._tabs_for_pane(pane_id))
                self._focus_active_pane()

    def _begin_page_shutdown(self, page: TerminalPage | RdpPage) -> None:
        """Keep a hidden page alive until its backend reports completion."""
        self._attention_pages.discard(page)
        if page in self._closing_pages:
            return
        self._closing_pages.add(page)
        # removeTab keeps QWidget ownership in the old pane. That pane may be
        # merged/deleted while its backend is still draining asynchronously.
        # Keep the hidden page under the workspace until ready_to_delete.
        page.hide()
        page.setParent(self)
        self._local_reconnect_pages.discard(page)
        self._connection_reconnect_profiles.pop(page, None)
        if page in self._command_contexts:
            context = self._command_contexts[page]
            self.command_terminal_closing.emit(page.session_id, context)
            self._command_contexts.pop(page, None)
        page.ready_to_delete.connect(self._finish_page_shutdown)
        page.shutdown()

    def _dispose_ssh_login_prompt(self, page: InlineTerminalPromptPage) -> None:
        """Forget and delete a prompt without publishing command lifecycle events."""
        self._ssh_login_prompt_contexts.pop(page, None)
        page.deleteLater()

    @Slot()
    def _finish_page_shutdown(self) -> None:
        """Delete a stopped page and finish an application-wide shutdown."""
        page = self.sender()
        if not isinstance(page, (TerminalPage, RdpPage)):
            return

        self._closing_pages.discard(page)
        self._attention_pages.discard(page)
        self._command_contexts.pop(page, None)
        page.deleteLater()
        if (
            self._shutdown_all_requested
            and not self._shutdown_all_in_progress
            and not self._closing_pages
        ):
            self.all_terminals_stopped.emit()

    def _open_terminal(
        self,
        terminal_name: str,
        backend: TerminalBackend,
        *,
        session_context: object | None = None,
        reconnect_profile: ConnectionProfile | None = None,
        local_reconnect: bool = False,
        local_start_directory: str | None = None,
        pane_id: UUID | None = None,
    ) -> TerminalPage:
        """Create, select, and start a tab through the shared lifecycle path."""
        try:
            tabs = self._tabs_for_pane(pane_id)
        except (TypeError, ValueError):
            backend.deleteLater()
            raise
        if reconnect_profile is not None and not isinstance(
            reconnect_profile,
            ConnectionProfile,
        ):
            backend.deleteLater()
            raise TypeError("reconnect_profile must be a ConnectionProfile or None")
        if not isinstance(local_reconnect, bool):
            backend.deleteLater()
            raise TypeError("local_reconnect must be a boolean")
        if local_reconnect and reconnect_profile is not None:
            backend.deleteLater()
            raise ValueError("A terminal cannot have local and connection reconnect plans")
        terminal_page = self._create_terminal_page(
            terminal_name,
            backend,
            reconnect_available=local_reconnect or reconnect_profile is not None,
        )
        terminal_page.configure_session_title(
            local=local_reconnect,
            local_start_directory=local_start_directory,
            ssh=reconnect_profile is not None and reconnect_profile.protocol == ConnectionProtocol.SSH,
            connection=not local_reconnect,
        )
        terminal_page.output_view.set_word_delete_sends_ctrl_w(
            reconnect_profile is not None and reconnect_profile.word_delete_mode is WordDeleteMode.CTRL_W
        )
        terminal_page.display_title_changed.connect(self._update_terminal_title)
        terminal_page.ssh_secret_submitted.connect(self._forward_ssh_secret_submitted)
        terminal_page.ssh_secret_cancel_requested.connect(self._forward_ssh_secret_cancel_requested)
        terminal_page.reconnect_requested.connect(self._handle_terminal_reconnect_requested)
        terminal_page.ended_close_requested.connect(self._handle_ended_terminal_close_requested)
        if local_reconnect:
            self._local_reconnect_pages.add(terminal_page)
        elif reconnect_profile is not None:
            self._connection_reconnect_profiles[terminal_page] = reconnect_profile
        if session_context is not None:
            self._command_contexts[terminal_page] = session_context
            terminal_page.process_started.connect(self._forward_command_terminal_started)
            terminal_page.process_finished.connect(self._forward_command_terminal_finished)
        previous_current_page = tabs.currentWidget()
        self._hide_start_page()
        tab_index = tabs.addTab(terminal_page, _qt_tab_label(terminal_page.display_title))
        protocol = (reconnect_profile.protocol.value if reconnect_profile is not None else
                    "local" if local_reconnect else "ssh" if session_context is not None else "terminal")
        self._bind_connection_color(terminal_page, reconnect_profile)
        self._set_session_icon(tabs, tab_index, protocol)
        tabs.setTabToolTip(tab_index, plain_text_tooltip(terminal_page.display_title))
        self._sync_tab_bar_visibility()
        tabs.setTabsClosable(True)
        tabs.setCurrentIndex(tab_index)
        try:
            terminal_page.start()
        except Exception:
            failed_owner = self.panes.owner_of(terminal_page)
            if failed_owner is not None:
                failed_owner.removeTab(failed_owner.indexOf(terminal_page))
            self._command_contexts.pop(terminal_page, None)
            self._local_reconnect_pages.discard(terminal_page)
            self._connection_reconnect_profiles.pop(terminal_page, None)
            if self.terminal_count == 0:
                self._show_empty_workspace()
            elif (
                previous_current_page is not None
                and (previous_owner := self.panes.owner_of(previous_current_page)) is not None
            ):
                previous_owner.setCurrentWidget(previous_current_page)
            self._begin_page_shutdown(terminal_page)
            raise
        self._publish_terminal_count()
        self._focus_new_session(terminal_page)
        return terminal_page

    def _create_terminal_page(
        self,
        terminal_name: str,
        backend: TerminalBackend,
        *,
        session_id: UUID | None = None,
        reconnect_available: bool = False,
    ) -> TerminalPage:
        """Build every real terminal with one snapshot of workspace preferences."""
        page = TerminalPage(
            terminal_name,
            backend,
            terminal_colors=self._terminal_colors,
            terminal_font=self._terminal_font_settings,
            scrollback_limit=self._terminal_scrollback_lines,
            session_id=session_id,
            reconnect_available=reconnect_available,
        )
        page.set_focus_guard(lambda: self._page_may_take_focus(page))
        page.set_visual_bell_enabled(self._visual_bell_enabled)
        page.output_view.set_highlighting(self._terminal_highlighting)
        page.output_view.set_backspace_sends_ctrl_h(self._backspace_sends_ctrl_h)
        page.output_view.set_grid_resize_guard(self._pane_grid_may_resize)
        page.bell_requested.connect(self._handle_terminal_bell_requested)
        page.process_finished.connect(self._handle_open_terminal_finished)
        page.backend.error_occurred.connect(self._handle_open_terminal_backend_error)
        page.output_export_requested.connect(self._forward_output_export_requested)
        page.saved_commands_requested.connect(self._forward_saved_commands_requested)
        page.input_lock_changed.connect(self._publish_open_sessions_changed)
        page.session_facts_changed.connect(self._publish_open_sessions_changed)
        for shortcut_target in (
            page.output_view,
            page.terminal_search_bar.query_input,
            page.ssh_secret_prompt.secret_input,
            page.command_input,
        ):
            if shortcut_target is not None:
                self._shortcut_event_filter.watch(shortcut_target)
        return page

    def _publish_terminal_count(self) -> None:
        """Publish real session-count transitions independent of current-tab changes."""
        self._publish_open_sessions_changed()
        terminal_count = self.terminal_count
        if terminal_count == self._published_terminal_count:
            return
        self._published_terminal_count = terminal_count
        self.terminal_count_changed.emit(terminal_count)

    @Slot(str)
    def _forward_ssh_login_submitted(self, username: str) -> None:
        """Forward a validated prompt result with its stable identity and context."""
        page = self.sender()
        if (
            not isinstance(page, InlineTerminalPromptPage)
            or page not in self._ssh_login_prompt_contexts
            or self.panes.owner_of(page) is None
        ):
            return
        self.ssh_login_submitted.emit(
            page.session_id,
            self._ssh_login_prompt_contexts[page],
            username,
        )

    @Slot()
    def _cancel_ssh_login_prompt(self) -> None:
        """Close the exact visible prompt that requested cancellation."""
        page = self.sender()
        if not isinstance(page, InlineTerminalPromptPage):
            return
        self._close_page(page)

    @Slot()
    def _handle_terminal_reconnect_requested(self) -> None:
        """Reconnect only the exact ended page that emitted the guarded request."""
        page = self.sender()
        if (
            not isinstance(page, TerminalPage)
            or self.panes.owner_of(page) is None
            or not page.session_ended
            or not page.reconnect_request_pending
        ):
            return
        self._publish_open_sessions_changed()
        if not self.accepts_new_terminals:
            page.finish_reconnect_request(succeeded=False)
            self._publish_open_sessions_changed()
            return

        if page in self._local_reconnect_pages:
            succeeded = (
                self.open_local_terminal(pane_id=self.pane_id_for_session(page.session_id))
                is not None
            )
            if self.panes.owner_of(page) is None:
                return
            if not page.finish_reconnect_request(succeeded=succeeded):
                return
            if succeeded:
                self._close_page(page)
            else:
                self._publish_open_sessions_changed()
            return

        profile = self._connection_reconnect_profiles.get(page)
        if profile is None:
            page.finish_reconnect_request(succeeded=False)
            self._publish_open_sessions_changed()
            return
        self.connection_reconnect_requested.emit(page.session_id, profile)

    @Slot()
    def _handle_ended_terminal_close_requested(self) -> None:
        """Close only the visible ended page that owns the clicked banner action."""
        page = self.sender()
        if not isinstance(page, TerminalPage) or not page.session_ended:
            return
        self._close_page(page)

    @Slot(object, str)
    def _forward_ssh_secret_submitted(self, request_id: object, secret: str) -> None:
        """Forward an inline secret without writing it into terminal output or context."""
        page = self.sender()
        if not isinstance(page, TerminalPage) or self.panes.owner_of(page) is None:
            return
        try:
            self.ssh_secret_submitted.emit(page.session_id, request_id, secret)
        finally:
            secret = ""

    @Slot(object)
    def _forward_ssh_secret_cancel_requested(self, request_id: object) -> None:
        """Forward cancellation only while the exact terminal remains visible."""
        page = self.sender()
        if not isinstance(page, TerminalPage) or self.panes.owner_of(page) is None:
            return
        self.ssh_secret_cancel_requested.emit(page.session_id, request_id)

    @Slot()
    def _handle_terminal_bell_requested(self) -> None:
        """Mark a live terminal only when its bell was not already being viewed."""
        page = self.sender()
        if (
            not self._visual_bell_enabled
            or not isinstance(page, TerminalPage)
            or self.panes.owner_of(page) is None
            or self._page_is_actively_viewed(page)
        ):
            return
        if page in self._attention_pages:
            return
        self._attention_pages.add(page)
        self._refresh_terminal_tab_label(page)

    @Slot(int)
    def _handle_current_tab_changed(self, _tab_index: int) -> None:
        """A selected terminal clears attention only while the app is actually visible."""
        page = self.tabs.currentWidget()
        if isinstance(page, TerminalPage) and self._page_is_actively_viewed(page):
            self._clear_terminal_attention(page)
        self.active_terminal_changed.emit()
        self._publish_open_sessions_changed()

    @Slot(object)
    def _handle_application_state_changed(self, state: object) -> None:
        """Clear the selected marker when the application returns to the foreground."""
        self._application_active = state == Qt.ApplicationState.ApplicationActive
        if not self._application_active:
            return
        self._window_active = self.window().isActiveWindow()
        self._clear_viewed_terminal_attention()

    @Slot(object)
    def _handle_focus_window_changed(self, _window: object) -> None:
        """Track whether this workspace's top-level window is the one being viewed."""
        self._window_active = self.window().isActiveWindow()
        if self._window_active:
            self._clear_viewed_terminal_attention()

    def _clear_viewed_terminal_attention(self) -> None:
        """Consume the current marker only when its owning window is actually active."""
        if not self._application_active or not self._window_active or not self.isVisible():
            return
        page = self.tabs.currentWidget()
        if isinstance(page, TerminalPage):
            self._clear_terminal_attention(page)

    def _page_is_actively_viewed(self, page: TerminalPage) -> bool:
        """Return whether one page is selected in a visible foreground workspace."""
        return bool(
            self._application_active
            and self._window_active
            and self.isVisible()
            and self.tabs.currentWidget() is page
        )

    def _clear_terminal_attention(self, page: TerminalPage) -> None:
        """Consume one page-identity marker without depending on its movable index."""
        if page not in self._attention_pages:
            return
        self._attention_pages.discard(page)
        self._refresh_terminal_tab_label(page)

    def _refresh_terminal_tab_label(self, page: TerminalPage) -> None:
        """Render visible and accessible state for a still-visible terminal page."""
        tabs = self.panes.owner_of(page)
        if tabs is None:
            return
        tab_index = tabs.indexOf(page)
        self._set_session_icon(tabs, tab_index, page.property("sessionProtocol") or "terminal")
        attention_requested = page in self._attention_pages
        tabs.setTabText(
            tab_index,
            _terminal_page_tab_label(
                page,
                attention_requested=attention_requested,
            ),
        )
        description = plain_text_tooltip(
            _terminal_page_tab_description(
                page,
                attention_requested=attention_requested,
            )
        )
        tabs.setTabToolTip(tab_index, description)
        tabs.setTabWhatsThis(tab_index, description)
        self._publish_open_sessions_changed()

    @Slot(PROCESS_EXIT_SIGNAL_TYPE)
    def _handle_open_terminal_finished(self, _exit_code: int) -> None:
        """Observe completion for local and command pages without reading output."""
        page = self.sender()
        if isinstance(page, TerminalPage):
            self._refresh_terminal_tab_label(page)

    @Slot(str)
    def _handle_open_terminal_backend_error(self, _message: str) -> None:
        """An error can dismiss authentication before the process finishes."""
        backend = self.sender()
        page = backend.parent() if isinstance(backend, TerminalBackend) else None
        if isinstance(page, TerminalPage):
            self._refresh_terminal_tab_label(page)

    @Slot(str)
    def _update_terminal_title(self, _display_title: str) -> None:
        """Update a movable tab by page identity and ignore hidden closing pages."""
        page = self.sender()
        if not isinstance(page, TerminalPage):
            return
        self._refresh_terminal_tab_label(page)

    @Slot(int)
    def _forward_command_terminal_started(self, process_id: int) -> None:
        """Forward the bound child PID with the command's opaque context."""
        page = self.sender()
        if not isinstance(page, TerminalPage) or page not in self._command_contexts:
            return
        self.command_terminal_started.emit(
            page.session_id,
            self._command_contexts[page],
            process_id,
        )

    @Slot(PROCESS_EXIT_SIGNAL_TYPE)
    def _forward_command_terminal_finished(self, exit_code: int) -> None:
        """Forward a command completion with its opaque application context."""
        page = self.sender()
        if not isinstance(page, TerminalPage) or page not in self._command_contexts:
            return
        context = self._command_contexts.pop(page)
        self.command_terminal_finished.emit(
            page.session_id,
            page.recent_output,
            context,
            exit_code,
        )

    def _page_for_session(self, session_id: UUID) -> TerminalPage | InlineTerminalPromptPage | RdpPage | None:
        """Resolve stable session identity across every pane, never a local index."""
        for page in self._all_pages():
            if (
                isinstance(page, (TerminalPage, InlineTerminalPromptPage, RdpPage))
                and page.session_id == session_id
            ):
                return page
        return None

    def _create_start_page(self) -> QWidget:
        """Create an actionable welcome page displayed without terminal sessions."""
        start_page = QWidget()
        start_page.setObjectName("startPage")
        start_layout = QVBoxLayout(start_page)
        start_layout.setContentsMargins(0, 0, 0, 0)
        start_layout.setSpacing(0)

        start_scroll = QScrollArea()
        start_scroll.setObjectName("startScrollArea")
        start_scroll.setFrameShape(QFrame.Shape.NoFrame)
        start_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        start_scroll.setWidgetResizable(True)
        start_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        start_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        start_scroll.viewport().setObjectName("startScrollViewport")

        start_canvas = QWidget()
        start_canvas.setObjectName("startCanvas")
        canvas_layout = QVBoxLayout(start_canvas)
        canvas_layout.setContentsMargins(40, 36, 40, 40)
        canvas_layout.setSpacing(0)

        content = QFrame()
        content.setObjectName("startContent")
        content.setMaximumWidth(680)
        content.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._start_content = content
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        identity_row = QHBoxLayout()
        identity_row.setContentsMargins(0, 0, 0, 0)
        identity_row.setSpacing(14)

        mark = QLabel(">_")
        mark.setObjectName("startBrandMark")
        mark.setAccessibleName(tr("Termana terminal symbol"))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(54, 54)
        scale_widget_font(mark, 1.55, weight=QFont.Weight.ExtraBold)

        identity_copy = QVBoxLayout()
        identity_copy.setContentsMargins(0, 2, 0, 0)
        identity_copy.setSpacing(2)

        eyebrow = QLabel(tr("TERMANA"))
        eyebrow.setObjectName("startEyebrow")
        scale_widget_font(eyebrow, 0.8, weight=QFont.Weight.ExtraBold)

        title = WrappingLabel(tr("Ready to connect"))
        title.setObjectName("emptyStateTitle")
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scale_widget_font(title, 2.0, weight=QFont.Weight.Bold)
        identity_copy.addWidget(eyebrow)
        identity_copy.addWidget(title)
        identity_row.addWidget(mark)
        identity_row.addLayout(identity_copy, 1)

        description = WrappingLabel(
            tr(
                "Open a local shell or a saved connection. Your sessions stay close at hand in the sidebar."
            )
        )
        description.setObjectName("startDescription")
        description.setTextFormat(Qt.TextFormat.PlainText)
        scale_widget_font(description, 1.08)

        actions = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        actions.setContentsMargins(0, 8, 0, 0)
        actions.setSpacing(10)
        self._start_actions_layout = actions

        self.start_new_connection_button = QPushButton(tr("New connection"))
        self.start_new_connection_button.setObjectName("startNewConnectionButton")
        self.start_new_connection_button.setAccessibleName(tr("Create a new saved connection"))
        self.start_new_connection_button.setToolTip(tr("Create a saved SSH or Telnet connection"))

        self.start_local_terminal_button = QPushButton(tr("Open local terminal"))
        self.start_local_terminal_button.setObjectName("startLocalTerminalButton")
        self.start_local_terminal_button.setAccessibleName(tr("Open a local terminal"))
        self.start_local_terminal_button.setToolTip(tr("Open a shell on this computer"))

        actions.addWidget(self.start_new_connection_button)
        actions.addWidget(self.start_local_terminal_button)

        content_layout.addLayout(identity_row)
        content_layout.addWidget(description)
        content_layout.addLayout(actions)
        content_layout.addSpacing(8)

        self.start_new_connection_button.clicked.connect(self.new_connection_requested)
        self.start_local_terminal_button.clicked.connect(self.local_terminal_requested)

        canvas_layout.addStretch()
        canvas_layout.addWidget(content, 0, Qt.AlignmentFlag.AlignCenter)
        canvas_layout.addStretch()
        start_scroll.setWidget(start_canvas)
        start_layout.addWidget(start_scroll)
        return start_page

    def resizeEvent(self, event) -> None:
        """Reflow welcome actions when the workspace or system font needs more room."""
        super().resizeEvent(event)
        if hasattr(self, "_start_content"):
            self._update_start_page_layout()

    def _update_start_page_layout(self) -> None:
        """Stack start controls only when their actual font-driven hints no longer fit."""
        available_width = max(0, self.width() - 80)
        action_width = (
            self.start_new_connection_button.sizeHint().width()
            + self.start_local_terminal_button.sizeHint().width()
            + self._start_actions_layout.spacing()
        )
        action_direction = (
            QBoxLayout.Direction.TopToBottom
            if action_width > available_width
            else QBoxLayout.Direction.LeftToRight
        )
        changed = self._start_actions_layout.direction() != action_direction
        if changed:
            self._start_actions_layout.setDirection(action_direction)

        if changed:
            self._start_content.updateGeometry()

    def _hide_start_page(self) -> None:
        """Remove the reusable Start page before opening a real tab."""
        tabs = self.panes.owner_of(self._start_page)
        if tabs is not None:
            tabs.removeTab(tabs.indexOf(self._start_page))
        # A tab removal does not transfer Qt ownership. The original pane can
        # be merged away long before this welcome page is needed again.
        self._start_page.hide()
        self._start_page.setParent(self)

    def _show_start_page(self) -> None:
        """Restore the reusable Start page and hide its close control."""
        self.panes.merge_all()
        if self.tabs.indexOf(self._start_page) < 0:
            self.tabs.addTab(self._start_page, tr(START_TAB_TITLE))
        self.tabs.setTabsClosable(False)
        self.tabs.setCurrentWidget(self._start_page)
        self._sync_tab_bar_visibility()

    def _show_empty_workspace(self) -> None:
        """Keep an empty multi-pane layout reusable until the user merges it."""
        if self.pane_count == 1:
            self._show_start_page()
        else:
            self._hide_start_page()
            self._sync_tab_bar_visibility()
            self.panes.focus_active_empty_pane()

    def _sync_tab_bar_visibility(self) -> None:
        """Hide chrome that adds no navigation value on the lone Start page."""
        for tabs in self.panes.tab_groups:
            tabs.tabBar().setVisible(self.terminal_count > 0)
