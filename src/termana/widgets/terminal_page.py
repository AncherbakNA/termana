"""User interface for one local terminal backend."""

from collections.abc import Callable
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont, QKeySequence, QResizeEvent, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from termana.dialogs.paste_preview_dialog import PastePreviewDialog
from termana.i18n import tr, validation_text
from termana.models.command_snippet import validate_snippet_command
from termana.models.settings import TerminalFontSettings
from termana.qt_lifecycle import schedule_delete_later
from termana.terminal.backend import PROCESS_EXIT_SIGNAL_TYPE, TerminalBackend
from termana.terminal.paste import (
    MAX_PASTE_PAYLOAD_BYTES,
    PasteTooLargeError,
    PreparedPaste,
    prepare_paste,
)
from termana.terminal.process_directory import DIRECTORY_READERS, process_directory
from termana.terminal.screen import (
    DEFAULT_SCROLLBACK_LINES,
    ScreenUpdate,
    TerminalScreenError,
    TerminalScreenModel,
    TerminalTextSnapshot,
)
from termana.terminal.search import TerminalSearchResult, search_terminal_screen
from termana.terminal.tab_title import validate_custom_tab_title
from termana.terminal.title import sanitize_terminal_title
from termana.theme import TerminalColors
from termana.ui_typography import scale_widget_font
from termana.widgets.connection_advisory import ConnectionAdvisory
from termana.widgets.inline_terminal_secret_prompt import InlineTerminalSecretPrompt
from termana.widgets.terminal_search_bar import FULL_SEARCH_BAR_WIDTH, TerminalSearchBar
from termana.widgets.terminal_view import TerminalView

RECENT_OUTPUT_LIMIT_BYTES: Final = 64 * 1024
TERMINAL_SEARCH_DEBOUNCE_MS: Final = 120
TERMINAL_SEARCH_MAX_LATENCY_MS: Final = 750
TERMINAL_SEARCH_OVERLAY_MARGIN: Final = 10
TERMINAL_SEARCH_OVERLAY_MAX_WIDTH: Final = 720
TERMINAL_VISUAL_BELL_DURATION_MS: Final = 200


class TerminalPage(QFrame):
    """Connect one terminal screen to one replaceable process backend."""

    ready_to_delete = Signal()
    process_finished = Signal(PROCESS_EXIT_SIGNAL_TYPE)
    process_started = Signal(int)
    display_title_changed = Signal(str)
    ssh_secret_submitted = Signal(object, str)
    ssh_secret_cancel_requested = Signal(object)
    reconnect_requested = Signal()
    ended_close_requested = Signal()
    bell_requested = Signal()
    output_export_requested = Signal()
    saved_commands_requested = Signal()
    group_input_stop_requested = Signal()
    input_lock_changed = Signal(bool)
    session_facts_changed = Signal()
    logging_changed = Signal()

    def __init__(
        self,
        terminal_name: str,
        backend: TerminalBackend,
        *,
        terminal_colors: TerminalColors | None = None,
        terminal_font: TerminalFontSettings | None = None,
        scrollback_limit: int = DEFAULT_SCROLLBACK_LINES,
        session_id: UUID | None = None,
        reconnect_available: bool = False,
        visual_bell_enabled: bool = True,
    ) -> None:
        """Build a terminal page around an injected backend."""
        super().__init__()

        if session_id is not None and not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID or None")
        if not isinstance(reconnect_available, bool):
            raise TypeError("reconnect_available must be a boolean")
        if type(visual_bell_enabled) is not bool:
            raise TypeError("visual_bell_enabled must be a boolean")

        self.setObjectName("terminalPage")
        self._session_id = uuid4() if session_id is None else session_id
        self.terminal_name = terminal_name
        self._display_title = terminal_name
        self.remote_window_title: str | None = None
        self._fixed_connection_title = False
        self._local_directory_title = False
        self._directory_future = None
        self._directory_pid = None
        self._directory_timer = QTimer(self)
        self._directory_timer.setInterval(700)
        self._directory_timer.timeout.connect(self._poll_local_directory)
        self._custom_tab_title: str | None = None
        self.backend = backend
        self.backend.setParent(self)
        self._is_shut_down = False
        self._backend_finished = False
        self._input_locked = False
        self._exit_code: int | None = None
        self._reconnect_available = reconnect_available
        self._session_end_action_pending = False
        self._session_logger = None
        self._ready_to_delete_emitted = False
        self._last_backend_size: tuple[int, int] | None = None
        self._last_backend_error: str | None = None
        self._recent_output = bytearray()
        self._active_ssh_secret_request_id: UUID | None = None
        self._active_ssh_secret_is_host_password: bool | None = None
        self._terminal_search_result = TerminalSearchResult()
        self._terminal_search_active_index: int | None = None
        self._terminal_search_should_scroll_on_refresh = False
        self._terminal_search_dirty = False
        self._visual_bell_enabled = visual_bell_enabled
        self._focus_guard: Callable[[], bool] = lambda: True
        self._starting_focus_widget: QWidget | None = None
        self._waiting_for_started = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.screen = TerminalScreenModel(
            encoding=self.backend.text_encoding,
            scrollback_limit=scrollback_limit,
        )
        self.output_view = TerminalView(
            self.screen,
            palette=terminal_colors,
            terminal_font=terminal_font,
            legacy_console_colors=backend.uses_legacy_console_colors,
        )

        self.visual_bell_frame = QFrame(self.output_view.viewport())
        self.visual_bell_frame.setObjectName("terminalVisualBellFrame")
        self.visual_bell_frame.setProperty("bellState", "inactive")
        self.visual_bell_frame.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.visual_bell_frame.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.visual_bell_frame.hide()
        self._position_visual_bell_frame()

        self.status_label = QLabel()
        self.status_label.setObjectName("terminalStatus")
        self.status_label.setAccessibleName(tr("Terminal status"))
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.hide()

        self.session_end_banner = QFrame(self)
        self.session_end_banner.setObjectName("terminalEndedBanner")
        self.session_end_banner.setAccessibleName(tr("Terminal session ended"))
        self._session_end_layout = QBoxLayout(
            QBoxLayout.Direction.LeftToRight,
            self.session_end_banner,
        )
        self._session_end_layout.setContentsMargins(16, 12, 16, 12)
        self._session_end_layout.setSpacing(14)

        session_end_copy = QVBoxLayout()
        session_end_copy.setContentsMargins(0, 0, 0, 0)
        session_end_copy.setSpacing(2)

        self.session_end_title = QLabel(tr("Session ended"))
        self.session_end_title.setObjectName("terminalEndedTitle")
        self.session_end_title.setTextFormat(Qt.TextFormat.PlainText)
        scale_widget_font(self.session_end_title, 1.08, weight=QFont.Weight.DemiBold)

        self.session_end_detail = QLabel()
        self.session_end_detail.setObjectName("terminalEndedDetail")
        self.session_end_detail.setAccessibleName(tr("Terminal session result"))
        self.session_end_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.session_end_detail.setWordWrap(True)
        self.session_end_detail.setMinimumWidth(0)
        self.session_end_detail.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )

        session_end_copy.addWidget(self.session_end_title)
        session_end_copy.addWidget(self.session_end_detail)
        self.session_end_keys = QLabel(self.session_end_banner)
        self.session_end_keys.setObjectName("mutedText")
        self.session_end_keys.setWordWrap(True)
        self.session_end_keys.setTextFormat(Qt.TextFormat.PlainText)
        self.session_end_keys.setText(
            tr("R — reconnect · S — save output · Space — close")
            if reconnect_available else tr("S — save output · Space — close")
        )
        session_end_copy.addWidget(self.session_end_keys)

        self._session_end_actions = QHBoxLayout()
        self._session_end_actions.setContentsMargins(0, 0, 0, 0)
        self._session_end_actions.setSpacing(8)
        self._session_end_actions.addStretch()

        # Visibility is configured before layout insertion. An unparented
        # visible button becomes a native top-level window and can switch the
        # user's macOS fullscreen Space before Qt reparents it into the banner.
        self.reconnect_button = QPushButton(tr("Reconnect"), self.session_end_banner)
        self.reconnect_button.setObjectName("terminalReconnectButton")
        self.reconnect_button.setAccessibleName(tr("Reconnect terminal session"))
        self.reconnect_button.setToolTip(
            tr("Start a fresh session; remote connections authenticate again")
        )
        self.reconnect_button.setAutoDefault(False)
        self.reconnect_button.setDefault(False)
        self.reconnect_button.setVisible(reconnect_available)

        self.ended_close_button = QPushButton(tr("Close"), self.session_end_banner)
        self.ended_close_button.setObjectName("terminalEndedCloseButton")
        self.ended_close_button.setAccessibleName(tr("Close ended terminal tab"))
        self.ended_close_button.setAutoDefault(False)
        self.ended_close_button.setDefault(False)

        self.ended_save_button = QPushButton(tr("Save output"), self.session_end_banner)
        self.ended_save_button.setObjectName("terminalEndedSaveButton")
        self.ended_save_button.setProperty("actionRole", "secondary")
        self.ended_save_button.setAutoDefault(False)
        self.ended_save_button.setDefault(False)
        self.ended_save_button.setAccessibleName(tr("Save ended terminal output"))

        self._session_end_actions.addWidget(self.reconnect_button)
        self._session_end_actions.addWidget(self.ended_save_button)
        self._session_end_actions.addWidget(self.ended_close_button)
        self._session_end_layout.addLayout(session_end_copy, 1)
        self._session_end_layout.addLayout(self._session_end_actions)
        self.session_end_banner.hide()

        self.terminal_search_bar = TerminalSearchBar()
        self.terminal_search_bar.setParent(self.output_view.viewport())

        self.ssh_secret_prompt = InlineTerminalSecretPrompt(
            terminal_colors=self.output_view.terminal_colors,
            terminal_font=self.output_view.terminal_font_settings,
        )
        self.ssh_secret_prompt.set_focus_guard(
            lambda: self._focus_guard() and self._may_restore_async_input_focus()
        )
        # A viewport child, never a footer or a transient top-level window.
        # Scrolling inside the overlay keeps long fingerprints readable even
        # in a small split pane without inserting secrets into the screen model.
        self.ssh_prompt_overlay = QScrollArea(self.output_view.viewport())
        self.ssh_prompt_overlay.setObjectName("terminalAuthenticationOverlay")
        self.ssh_prompt_overlay.setFrameShape(QFrame.Shape.NoFrame)
        self.ssh_prompt_overlay.setWidgetResizable(True)
        self.ssh_prompt_overlay.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ssh_prompt_overlay.setWidget(self.ssh_secret_prompt)
        self.ssh_prompt_overlay.viewport().setObjectName("terminalAuthenticationViewport")
        self._apply_ssh_prompt_overlay_colors(self.output_view.terminal_colors)
        self.ssh_prompt_overlay.hide()

        self.command_input: QLineEdit | None = None
        if not self.backend.supports_direct_input:
            self.command_input = QLineEdit()
            self.command_input.setObjectName("terminalCommandInput")
            self.command_input.setAccessibleName(tr("Terminal command"))
            self.command_input.setPlaceholderText(tr("Enter a command and press Return"))
            self.command_input.setFont(self.output_view.font())
            self.command_input.setEnabled(False)

        self.connection_advisory = ConnectionAdvisory()
        layout.addWidget(self.status_label)
        layout.addWidget(self.session_end_banner)
        layout.addWidget(self.connection_advisory)
        self.output_view.set_input_lock_state(False)
        self.group_input_banner = QFrame()
        group_layout = QHBoxLayout(self.group_input_banner)
        group_layout.setContentsMargins(8, 4, 8, 4)
        self.group_input_label = QLabel(
            tr("GROUP KEYBOARD INPUT ON · Disable before entering passwords or secrets")
        )
        self.group_input_label.setTextFormat(Qt.TextFormat.PlainText)
        self.group_input_label.setWordWrap(True)
        group_layout.addWidget(self.group_input_label, 1)
        self.group_input_stop_button = QPushButton(tr("Stop group"))
        self.group_input_stop_button.setAutoDefault(False)
        self.group_input_stop_button.setAccessibleName(tr("Stop SSH group keyboard input"))
        self.group_input_stop_button.clicked.connect(self.group_input_stop_requested)
        group_layout.addWidget(self.group_input_stop_button)
        self.group_input_banner.setAccessibleName(tr("SSH group keyboard input enabled"))
        self.group_input_banner.hide()
        layout.addWidget(self.group_input_banner)
        layout.addWidget(self.output_view, 1)
        if self.command_input is not None:
            layout.addWidget(self.command_input)

        self._terminal_search_timer = QTimer(self)
        self._terminal_search_timer.setObjectName("terminalSearchRefreshTimer")
        self._terminal_search_timer.setInterval(TERMINAL_SEARCH_DEBOUNCE_MS)
        self._terminal_search_timer.setSingleShot(True)
        self._terminal_search_max_timer = QTimer(self)
        self._terminal_search_max_timer.setObjectName("terminalSearchMaxLatencyTimer")
        self._terminal_search_max_timer.setInterval(TERMINAL_SEARCH_MAX_LATENCY_MS)
        self._terminal_search_max_timer.setSingleShot(True)
        self._visual_bell_timer = QTimer(self)
        self._visual_bell_timer.setObjectName("terminalVisualBellTimer")
        self._visual_bell_timer.setInterval(TERMINAL_VISUAL_BELL_DURATION_MS)
        self._visual_bell_timer.setSingleShot(True)

        self.find_terminal_shortcut = QShortcut(self)
        self.find_terminal_shortcut.setKeys(QKeySequence.keyBindings(QKeySequence.StandardKey.Find))
        self.find_terminal_shortcut.setObjectName("findInTerminalShortcut")
        self.find_terminal_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.find_terminal_shortcut.setAutoRepeat(False)

        # These single-letter bindings exist only on the ended terminal surface,
        # never in another pane, authentication editor, search box or live shell.
        self._ended_shortcuts = []
        for key, action in (("R", "reconnect"), ("S", "save"), ("Space", "close")):
            shortcut = QShortcut(QKeySequence(key), self.output_view)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.setEnabled(False)
            shortcut.activated.connect(lambda action=action: self._ended_action(action))
            self._ended_shortcuts.append(shortcut)

        if self.command_input is not None:
            self.command_input.returnPressed.connect(self._send_command)
        self.output_view.input_generated.connect(self._send_direct_input)
        self.output_view.paste_requested.connect(self._handle_paste_request)
        self.output_view.one_line_paste_requested.connect(self._handle_one_line_paste_request)
        self.output_view.input_lock_requested.connect(self.set_input_locked)
        self.output_view.find_requested.connect(self._open_terminal_search)
        self.output_view.context_menu_closed.connect(self._restore_secret_focus_after_menu)
        self.output_view.grid_size_changed.connect(self._send_terminal_size)
        self.output_view.viewport_resized.connect(self._position_terminal_search_bar)
        self.output_view.output_export_requested.connect(self._request_output_export)
        self.output_view.saved_commands_requested.connect(self._request_saved_commands)
        self.output_view.viewport_resized.connect(self._position_visual_bell_frame)
        self.output_view.document_changed.connect(self._schedule_terminal_search_refresh)
        self.output_view.viewport_resized.connect(self._position_ssh_secret_prompt)
        self.output_view.document_changed.connect(self._position_ssh_secret_prompt)
        self.output_view.verticalScrollBar().valueChanged.connect(self._position_ssh_secret_prompt)
        self.ssh_secret_prompt.geometry_changed.connect(self._position_ssh_secret_prompt)
        self.terminal_search_bar.query_changed.connect(self._handle_terminal_search_query_changed)
        self.terminal_search_bar.options_changed.connect(
            lambda: self._handle_terminal_search_query_changed(
                self.terminal_search_bar.query_input.text()
            )
        )
        self.terminal_search_bar.next_requested.connect(lambda: self._navigate_terminal_search(1))
        self.terminal_search_bar.previous_requested.connect(
            lambda: self._navigate_terminal_search(-1)
        )
        self.terminal_search_bar.close_requested.connect(self._handle_terminal_search_closed)
        self._terminal_search_timer.timeout.connect(self._refresh_terminal_search)
        self._terminal_search_max_timer.timeout.connect(self._refresh_terminal_search)
        self._visual_bell_timer.timeout.connect(self._clear_visual_bell_flash)
        self.find_terminal_shortcut.activated.connect(self._open_terminal_search)
        self.ssh_secret_prompt.submitted.connect(self._forward_ssh_secret)
        self.ssh_secret_prompt.cancel_requested.connect(self._forward_ssh_secret_cancel)
        self.reconnect_button.clicked.connect(self._request_reconnect)
        self.ended_close_button.clicked.connect(self._request_ended_close)
        self.ended_save_button.clicked.connect(lambda: self._ended_action("save"))
        self._connect_backend()

    def _connect_backend(self) -> None:
        backend = self.backend
        self._backend_connections = []
        connections = [
            (backend.data_received, self._handle_data),
            (backend.started, self._handle_started),
            (backend.finished, self._handle_finished),
            (backend.warning_occurred, self._handle_warning),
            (backend.error_occurred, self._handle_error),
        ]
        negotiation_changed = getattr(backend, "negotiation_changed", None)
        if negotiation_changed is not None:
            connections.append((negotiation_changed, self.session_facts_changed.emit))
        for signal, slot in connections:
            # A disconnected queued Qt callback can lose QObject.sender().
            # Capture identity explicitly instead of treating None as current.
            def forward(*args, source=backend, receiver=slot):
                if self.backend is source:
                    receiver(*args)

            signal.connect(forward)
            self._backend_connections.append((signal, forward))

    def begin_host_key_recovery(self) -> bool:
        """Reserve this ended page while its explicit trust decision is applied."""
        if self._is_shut_down or not self._backend_finished or self._session_end_action_pending:
            return False
        self._session_end_action_pending = True
        self.reconnect_button.setText(tr("Updating key…"))
        self.reconnect_button.setEnabled(False)
        self.ended_close_button.setEnabled(False)
        return True

    def replace_ended_backend(self, backend: TerminalBackend) -> bool:
        """Adopt a cold compatible transport without discarding this tab's screen."""
        if not isinstance(backend, TerminalBackend):
            raise TypeError("backend must be a TerminalBackend")
        if (
            self._is_shut_down
            or not self._backend_finished
            or self.backend.is_running
            or backend is self.backend
            or backend.is_running
            or backend.supports_direct_input != self.backend.supports_direct_input
            or backend.text_encoding != self.backend.text_encoding
        ):
            return False
        previous = self.backend
        for signal, callback in self._backend_connections:
            signal.disconnect(callback)
        self.backend = backend
        backend.setParent(self)
        self.output_view.set_legacy_console_colors(backend.uses_legacy_console_colors)
        self._backend_finished = False
        self._exit_code = None
        self._session_end_action_pending = False
        for shortcut in self._ended_shortcuts:
            shortcut.setEnabled(False)
        self._last_backend_size = None
        self._last_backend_error = None
        self.session_end_banner.hide()
        self.connection_advisory.hide()
        self.reconnect_button.setText(tr("Reconnect"))
        self._connect_backend()
        previous.close()
        previous.deleteLater()
        try:
            self.output_view.synchronize_grid_size()
            self._show_status(tr("[{p0}] Verifying reviewed SSH key…", p0=f"{self.terminal_name}"))
            self._send_terminal_size(self.screen.columns, self.screen.rows)
            self._starting_focus_widget = QApplication.focusWidget()
            self._waiting_for_started = True
            backend.start()
        except Exception:
            backend.close()
            self._handle_error(tr("Could not restart SSH after the key update."))
            self._handle_finished(255)
            raise
        return True

    def acknowledge_initial_input_focus(self) -> None:
        """Retire the launching control's focus claim after explicit tab handoff."""
        focus = QApplication.focusWidget()
        if focus is not None and (focus is self or self.isAncestorOf(focus)):
            self._starting_focus_widget = focus

    def start(self, *, initial_output: bytes = b"") -> None:
        """Start without a transient banner; errors retain their own visible path."""
        if not isinstance(initial_output, bytes):
            raise TypeError("initial_output must be bytes")
        self._recent_output.clear()
        self.output_view.synchronize_grid_size()
        if initial_output:
            self._handle_data(initial_output)
        self._send_terminal_size(self.screen.columns, self.screen.rows)
        self._starting_focus_widget = QApplication.focusWidget()
        self._waiting_for_started = True
        self.backend.start()

    def set_focus_guard(self, guard: Callable[[], bool]) -> None:
        """Scope automatic input focus to the owner's currently active page."""
        if not callable(guard):
            raise TypeError("guard must be callable")
        self._focus_guard = guard

    def shutdown(self) -> None:
        """Stop the backend at most once."""
        if self._is_shut_down:
            return
        self._is_shut_down = True
        self.stop_session_logging()
        self._directory_timer.stop()
        if self._directory_future is not None:
            self._directory_future.cancel()
            self._directory_future = None
        self.output_view.set_input_lock_state(self._input_locked, available=False)
        self.output_view.set_output_export_enabled(False)
        self.find_terminal_shortcut.setEnabled(False)
        self._clear_visual_bell_flash()
        self.output_view.set_find_enabled(False)
        self._close_terminal_search(restore_focus=False)
        self._disable_input()
        if self._backend_finished or not self.backend.is_running:
            self.backend.close()
            self._emit_ready_to_delete()
            return
        self.backend.close()

    @property
    def output_export_available(self) -> bool:
        """Allow retained output export, including ended sessions, but never secret entry."""
        return not self._is_shut_down and self._active_ssh_secret_request_id is None

    @property
    def logging_active(self) -> bool:
        return self._session_logger is not None and self._session_logger.accepting

    @property
    def logging_available(self) -> bool:
        return (
            not self._is_shut_down and not self._backend_finished
            and not self._waiting_for_started and self.backend.is_running
            and self._active_ssh_secret_request_id is None
            and self._session_logger is None
        )

    def start_session_logging(self, root: Path) -> bool:
        if not self.logging_available:
            return False
        from termana.session_logging import SessionLogWriter
        logger = SessionLogWriter(root, self.backend.text_encoding, self)
        logger.failed.connect(self._session_log_failed)
        logger.finished.connect(self._session_log_finished)
        self._session_logger = logger
        try:
            logger.start()
        except Exception:
            self._session_logger = None
            logger.deleteLater()
            return False
        self.logging_changed.emit()
        return True

    def stop_session_logging(self) -> None:
        if self._session_logger is not None:
            self._session_logger.stop()
            self.logging_changed.emit()

    def _session_log_failed(self, message: str) -> None:
        if not self._is_shut_down:
            self._show_status(tr(message), is_warning=True)
        self.logging_changed.emit()

    def _session_log_finished(self) -> None:
        logger, self._session_logger = self._session_logger, None
        if logger is not None:
            logger.deleteLater()
        self.logging_changed.emit()
        if self._is_shut_down and (self._backend_finished or not self.backend.is_running):
            self._emit_ready_to_delete()

    def capture_output_snapshot(self) -> TerminalTextSnapshot | None:
        """Detach screen text only after rechecking the active page's permission."""
        if not self.output_export_available:
            return None
        return self.screen.capture_text_snapshot()

    def _request_output_export(self) -> None:
        if self.output_export_available:
            self.output_export_requested.emit()

    @property
    def command_insertion_available(self) -> bool:
        """Allow a saved command only through an already-ready non-secret input path."""
        if (
            self._is_shut_down
            or self._input_locked
            or self._backend_finished
            or self._active_ssh_secret_request_id is not None
            or not self.backend.is_running
        ):
            return False
        if self.backend.supports_direct_input:
            return self.output_view.input_enabled
        return self.command_input is not None and self.command_input.isEnabled()

    @property
    def input_locked(self) -> bool:
        """The session-local regular-input policy, never authentication consent."""
        return self._input_locked

    def set_input_locked(self, locked: bool) -> None:
        """Keep reading/copy/search and exact inline authentication usable."""
        if type(locked) is not bool:
            raise TypeError("locked must be a boolean")
        if self._is_shut_down or self._input_locked == locked:
            return
        # Release any child-owned mouse gesture before locking its input path.
        if locked:
            self._disable_regular_input()
        self._input_locked = locked
        self.output_view.set_input_lock_state(locked)
        if not locked:
            self._enable_regular_input(restore_focus=False)
        self.input_lock_changed.emit(locked)

    def _request_saved_commands(self) -> None:
        if self.command_insertion_available:
            self.saved_commands_requested.emit()

    def insert_saved_command(self, command: str) -> bool:
        """Insert exactly the requested lines, including an explicit final Enter."""
        try:
            validate_snippet_command(command)
        except (ValueError, TypeError):
            return False
        if not self.command_insertion_available:
            return False
        command = command.replace("\r\n", "\n").replace("\r", "\n")
        if not self.backend.supports_direct_input:
            if "\n" in command:
                return False
            editor = self.command_input
            candidate_length = (
                len(editor.text().encode("utf-16-le"))
                - len(editor.selectedText().encode("utf-16-le"))
                + len(command.encode("utf-16-le"))
            ) // 2
            if candidate_length > editor.maxLength():
                return False
            editor.insert(command)
            return True
        # Explicit saved multiline commands are a reviewed command sequence,
        # not clipboard paste. Bracketing would suppress the requested Enter
        # separators in supported shells. Never add or strip a final submission.
        prepared = prepare_paste(command, bracketed=self.screen.bracketed_paste and "\n" not in command)
        try:
            payload = prepared.payload.encode(self.backend.text_encoding, errors="strict")
        except UnicodeError:
            return False
        self.output_view.clear_selection()
        self._send_direct_bytes(payload)
        return True

    @property
    def session_id(self) -> UUID:
        """Return the immutable application identity of this terminal session."""
        return self._session_id

    @property
    def recent_output(self) -> bytes:
        """Return a bounded raw tail for transport-level diagnostics."""
        return bytes(self._recent_output)

    @property
    def display_title(self) -> str:
        """Return the current safe tab label without changing session identity."""
        return self._custom_tab_title or self.automatic_display_title

    @property
    def automatic_display_title(self) -> str:
        """Return the latest automatic label, including any sanitized OSC title."""
        return self._display_title

    @property
    def custom_tab_title(self) -> str | None:
        """Return this page's optional session-local tab label override."""
        return self._custom_tab_title

    def set_custom_tab_title(self, title: str | None) -> None:
        """Set a validated override, or restore the latest automatic label."""
        normalized = None if title is None else validate_custom_tab_title(title)
        if normalized == self._custom_tab_title:
            return
        self._custom_tab_title = normalized
        self.display_title_changed.emit(self.display_title)

    @property
    def requires_close_confirmation(self) -> bool:
        """Return whether a user close could interrupt this terminal session."""
        return not self._is_shut_down and not self._backend_finished

    @property
    def session_ended(self) -> bool:
        """Return whether this page has received its terminal backend result."""
        return self._backend_finished

    @property
    def exit_code(self) -> int | None:
        """The backend's first completion result, never parsed from terminal text."""
        return self._exit_code

    @property
    def reconnect_request_pending(self) -> bool:
        """Return whether the owner is resolving this page's reconnect request."""
        return self._session_end_action_pending

    @property
    def visual_bell_enabled(self) -> bool:
        """Return whether BEL requests currently flash the terminal frame."""
        return self._visual_bell_enabled

    @property
    def visual_bell_active(self) -> bool:
        """Return whether the bounded visual BEL flash is currently armed."""
        return self._visual_bell_timer.isActive() and not self.visual_bell_frame.isHidden()

    def set_visual_bell_enabled(self, enabled: bool) -> None:
        """Apply the visual-BEL preference without changing terminal focus or input."""
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        self._visual_bell_enabled = enabled
        if not enabled:
            self._clear_visual_bell_flash()

    def finish_reconnect_request(self, *, succeeded: bool) -> bool:
        """Resolve one pending reconnect attempt without restarting this old backend."""
        if not isinstance(succeeded, bool):
            raise TypeError("succeeded must be a boolean")
        if not self._session_end_action_pending or self._is_shut_down:
            return False
        if succeeded:
            return True
        self._session_end_action_pending = False
        self.reconnect_button.setText(tr("Reconnect"))
        self.reconnect_button.setEnabled(self._reconnect_available)
        self.ended_close_button.setEnabled(True)
        return True

    @property
    def active_ssh_secret_request_id(self) -> UUID | None:
        """Return the exact inline AskPass request currently owned by this page."""
        return self._active_ssh_secret_request_id

    @property
    def ssh_secret_tab_label(self) -> str | None:
        """Return a short, non-secret marker for the pending AskPass request."""
        if self._active_ssh_secret_request_id is None:
            return None
        return "Password" if self._active_ssh_secret_is_host_password else tr("SSH auth")

    def begin_ssh_secret_prompt(
        self,
        request_id: UUID,
        prompt: str,
        *,
        is_host_password: bool = True,
        is_host_confirmation: bool = False,
    ) -> bool:
        """Show one terminal-owned secret prompt without writing to the child PTY."""
        if not isinstance(request_id, UUID):
            raise TypeError("request_id must be a UUID")
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        if not isinstance(is_host_password, bool):
            raise TypeError("is_host_password must be a boolean")
        if type(is_host_confirmation) is not bool or (is_host_password and is_host_confirmation):
            raise ValueError("Host confirmation must be a non-password request")
        if (
            self._active_ssh_secret_request_id is not None
            or self._is_shut_down
            or self._backend_finished
            or not self.backend.is_running
        ):
            return False
        restore_focus = self._may_restore_async_input_focus()
        if self.logging_active:
            self.stop_session_logging()
            self._show_status(tr("Session logging stopped before authentication input."), is_warning=True)
        self.find_terminal_shortcut.setEnabled(False)
        self.output_view.set_find_enabled(False)
        self._close_terminal_search(restore_focus=False)
        self._active_ssh_secret_request_id = request_id
        self.output_view.set_output_export_enabled(False)
        self._active_ssh_secret_is_host_password = is_host_password
        self._disable_regular_input()
        self.output_view.clear_selection()
        self.ssh_secret_prompt.begin(
            prompt,
            is_host_password=is_host_password,
            is_host_confirmation=is_host_confirmation,
            restore_focus=False,
        )
        self._position_ssh_secret_prompt()
        self.ssh_prompt_overlay.show()
        self.ssh_prompt_overlay.raise_()
        self.ssh_secret_prompt.layout().activate()
        self._position_ssh_secret_prompt()
        self.ssh_prompt_overlay.ensureWidgetVisible(self.ssh_secret_prompt.secret_input)
        if restore_focus and self.isVisible() and self._focus_guard():
            self.ssh_secret_prompt.secret_input.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def finish_ssh_secret_prompt(self, request_id: UUID) -> bool:
        """Clear and hide only the exact inline AskPass request that completed."""
        if not isinstance(request_id, UUID):
            raise TypeError("request_id must be a UUID")
        if self._active_ssh_secret_request_id != request_id:
            return False
        restore_focus = self._may_restore_async_input_focus()
        # Keep only the sanitized question in terminal history. Never copy the
        # editor's contents, including non-password authentication responses.
        question = self.ssh_secret_prompt.prompt_label.text()
        self._active_ssh_secret_request_id = None
        self.output_view.set_output_export_enabled(not self._is_shut_down)
        self._active_ssh_secret_is_host_password = None
        self.ssh_secret_prompt.finish()
        self.ssh_prompt_overlay.hide()
        # A carriage return alone would overwrite an unterminated output line.
        # Reuse an empty cursor row, otherwise preserve it and start below it.
        cursor_line = self.screen.document_line_text(
            self.screen.scrollback_size + self.screen.cursor.row
        )
        separator = "\r\n" if cursor_line.rstrip() else "\r"
        self.append_application_diagnostic(
            (separator + question.replace("\n", "\r\n") + "\r\n").encode("utf-8")
        )
        if not self._is_shut_down:
            self.find_terminal_shortcut.setEnabled(True)
            self.output_view.set_find_enabled(True)
        self._enable_regular_input(restore_focus=restore_focus)
        return True

    def append_application_diagnostic(self, data: bytes) -> None:
        """Display a bounded trusted local diagnostic after backend redirection."""
        if not isinstance(data, bytes):
            raise TypeError("Terminal diagnostics must be bytes")
        if not data:
            return
        bounded = data[-RECENT_OUTPUT_LIMIT_BYTES:]
        separator = b"" if bounded.startswith((b"\r", b"\n")) else b"\r\n"
        payload = separator + bounded
        self._recent_output.extend(payload)
        overflow = len(self._recent_output) - RECENT_OUTPUT_LIMIT_BYTES
        if overflow > 0:
            del self._recent_output[:overflow]
        text = payload.decode(self.backend.text_encoding, errors="replace")
        try:
            update = self.output_view.feed_text(text)
        except TerminalScreenError as error:
            self._show_status(tr("Terminal parser error: {p0}", p0=f"{error}"), is_error=True)
            return
        self._apply_window_title(update.window_title)

    @property
    def terminal_font_settings(self) -> TerminalFontSettings:
        """Return the portable font used by output and fallback input."""
        return self.output_view.terminal_font_settings

    def set_terminal_colors(self, colors: TerminalColors) -> None:
        """Apply application theme colors without touching process state."""
        self.output_view.set_terminal_colors(colors)
        self.ssh_secret_prompt.set_terminal_colors(colors)
        self._apply_ssh_prompt_overlay_colors(colors)
        # Application stylesheet repolishing can restore Qt's original widget
        # font. Reconcile the rendered font even when the preference is unchanged.
        self.set_terminal_font(self.terminal_font_settings)

    def _apply_ssh_prompt_overlay_colors(self, colors: TerminalColors) -> None:
        """Keep the scroll viewport behind the prompt on the terminal surface."""
        self.ssh_prompt_overlay.viewport().setStyleSheet(
            f"QWidget#terminalAuthenticationViewport {{ background-color: {colors.background}; }}"
        )

    def set_terminal_font(self, settings: TerminalFontSettings) -> None:
        """Apply one font to output and compatibility command input."""
        self.output_view.set_terminal_font(settings)
        self.ssh_secret_prompt.set_terminal_font(settings)
        self._position_ssh_secret_prompt()
        if self.command_input is not None:
            self.command_input.setFont(self.output_view.font())

    def _position_ssh_secret_prompt(self, *_args: object) -> None:
        """Place the trusted request at the output cursor; bound it to the viewport."""
        if self._active_ssh_secret_request_id is None:
            return
        viewport = self.output_view.viewport()
        margin = TERMINAL_SEARCH_OVERLAY_MARGIN
        width = max(1, viewport.width() - 2 * margin)
        available = max(1, viewport.height() - 2 * margin)
        # Reserve scrollbar width in the wrap calculation so a tall question
        # cannot oscillate between scrollbar/no-scrollbar layouts.
        inner_width = max(1, width - self.ssh_prompt_overlay.verticalScrollBar().sizeHint().width())
        self.ssh_secret_prompt.prepare_width(inner_width)
        desired = self.ssh_secret_prompt.layout().totalHeightForWidth(inner_width)
        height = min(available, max(self.ssh_secret_prompt.minimumSizeHint().height(), desired))
        cursor_row = self.screen.scrollback_size + self.screen.cursor.row
        visible_row = cursor_row - self.output_view.verticalScrollBar().value()
        y = self.output_view.cell_rect(visible_row, 0).top()
        y = min(max(margin, y), max(margin, viewport.height() - margin - height))
        self.ssh_prompt_overlay.setGeometry(margin, y, width, height)
        self.ssh_secret_prompt.layout().activate()
        self.ssh_prompt_overlay.viewport().setPalette(self.ssh_secret_prompt.palette())

    def _open_terminal_search(self) -> None:
        """Reveal terminal search unless an SSH secret currently owns focus."""
        if self._active_ssh_secret_request_id is not None or self._is_shut_down:
            return
        self._position_terminal_search_bar()
        self.terminal_search_bar.open()

    def _restore_secret_focus_after_menu(self) -> None:
        """Return focus to an active SSH secret after an output popup closes."""
        if (
            self._active_ssh_secret_request_id is not None
            and self.ssh_secret_prompt.isVisible()
            and self.ssh_secret_prompt.secret_input.isEnabled()
            and self._focus_guard()
        ):
            self.ssh_secret_prompt.secret_input.setFocus(Qt.FocusReason.OtherFocusReason)

    def _position_terminal_search_bar(self) -> None:
        """Keep the overlay inside the terminal and away from the active match."""
        viewport = self.output_view.viewport()
        available_width = max(1, viewport.width() - 2 * TERMINAL_SEARCH_OVERLAY_MARGIN)
        available_height = max(1, viewport.height() - 2 * TERMINAL_SEARCH_OVERLAY_MARGIN)
        size_hint = self.terminal_search_bar.sizeHint()
        preferred_width = max(size_hint.width(), FULL_SEARCH_BAR_WIDTH)
        width = min(preferred_width, TERMINAL_SEARCH_OVERLAY_MAX_WIDTH, available_width)
        height = min(size_hint.height(), available_height)
        self.terminal_search_bar.resize(width, height)

        left = min(TERMINAL_SEARCH_OVERLAY_MARGIN, max(0, viewport.width() - width))
        right = max(0, viewport.width() - width - TERMINAL_SEARCH_OVERLAY_MARGIN)
        top = min(TERMINAL_SEARCH_OVERLAY_MARGIN, max(0, viewport.height() - height))
        bottom = max(0, viewport.height() - height - TERMINAL_SEARCH_OVERLAY_MARGIN)
        candidates = ((right, top), (right, bottom), (left, top), (left, bottom))
        active_rect = self._active_terminal_search_viewport_rect()
        chosen = candidates[0]
        if active_rect is not None:
            for candidate in candidates:
                if not QRect(*candidate, width, height).intersects(active_rect):
                    chosen = candidate
                    break
        self.terminal_search_bar.move(*chosen)

    def _active_terminal_search_viewport_rect(self) -> QRect | None:
        """Return the visible active match rectangle in viewport coordinates."""
        active_index = self._terminal_search_active_index
        matches = self._terminal_search_result.matches
        if active_index is None or not 0 <= active_index < len(matches):
            return None
        match = matches[active_index]
        viewport_row = match.row - self.output_view.verticalScrollBar().value()
        if not 0 <= viewport_row < self.screen.rows:
            return None
        return self.output_view.cell_rect(
            viewport_row,
            match.start_column,
        ).united(
            self.output_view.cell_rect(
                viewport_row,
                match.end_column,
            )
        )

    def _handle_terminal_search_query_changed(self, query: str) -> None:
        """Debounce a new literal query and immediately remove stale highlights."""
        self._terminal_search_timer.stop()
        self._terminal_search_max_timer.stop()
        self._terminal_search_result = TerminalSearchResult()
        self._terminal_search_active_index = None
        self._terminal_search_should_scroll_on_refresh = bool(query)
        self._terminal_search_dirty = bool(query)
        self.output_view.clear_search_matches()
        if query:
            self.terminal_search_bar.set_search_pending()
        else:
            self.terminal_search_bar.reset_match_status()
        if (
            query
            and self._active_ssh_secret_request_id is None
            and not self._is_shut_down
            and self.isVisible()
        ):
            self._terminal_search_timer.start()
            self._terminal_search_max_timer.start()

    def _schedule_terminal_search_refresh(
        self,
        update: ScreenUpdate,
        full_invalidation: bool,
    ) -> None:
        """Coalesce live terminal updates while an active query is visible."""
        if self.terminal_search_bar.query_input.text():
            self._retain_safe_terminal_search_matches(
                update,
                full_invalidation=full_invalidation,
            )
            self._terminal_search_dirty = True
            self.terminal_search_bar.set_search_pending(updating=True)
        if (
            self.terminal_search_bar.isHidden()
            or not self.terminal_search_bar.query_input.text()
            or self._active_ssh_secret_request_id is not None
            or self._is_shut_down
            or not self.isVisible()
        ):
            return
        # The short timer is trailing debounce; the second timer bounds latency
        # so a continuous log stream cannot starve search forever.
        self._terminal_search_timer.start()
        if not self._terminal_search_max_timer.isActive():
            self._terminal_search_max_timer.start()

    def _retain_safe_terminal_search_matches(
        self,
        update: ScreenUpdate,
        *,
        full_invalidation: bool,
    ) -> None:
        """Keep only highlights whose physical document rows did not change."""
        current_matches = self.output_view.search_matches
        if not current_matches:
            return
        if full_invalidation:
            self.output_view.clear_search_matches()
            return

        live_start = self.screen.scrollback_size
        invalid_rows = {live_start + row for row in update.dirty_rows}
        safe_matches = tuple(match for match in current_matches if match.row not in invalid_rows)
        if safe_matches == current_matches:
            return

        page_active_index = self._terminal_search_active_index
        page_matches = self._terminal_search_result.matches
        active_match = (
            page_matches[page_active_index]
            if page_active_index is not None and page_active_index < len(page_matches)
            else None
        )
        safe_active_index = (
            safe_matches.index(active_match)
            if active_match is not None and active_match in safe_matches
            else None
        )
        self.output_view.set_search_matches(safe_matches, safe_active_index)

    def _refresh_terminal_search(self) -> None:
        """Recompute bounded search results and preserve a stable active match."""
        self._terminal_search_timer.stop()
        self._terminal_search_max_timer.stop()
        query = self.terminal_search_bar.query_input.text()
        if (
            not query
            or self.terminal_search_bar.isHidden()
            or self._active_ssh_secret_request_id is not None
            or self._is_shut_down
            or not self.isVisible()
        ):
            self.output_view.clear_search_matches()
            return
        if not self._terminal_search_dirty:
            return

        previous_matches = self._terminal_search_result.matches
        previous_index = self._terminal_search_active_index
        previous_active = (
            previous_matches[previous_index]
            if previous_index is not None and previous_index < len(previous_matches)
            else None
        )
        result = search_terminal_screen(
            self.screen,
            query,
            case_sensitive=self.terminal_search_bar.match_case_button.isChecked(),
            whole_words=self.terminal_search_bar.whole_words_button.isChecked(),
        )
        self._terminal_search_result = result
        self._terminal_search_dirty = False

        if not result.matches:
            active_index = None
        elif previous_active is not None and previous_active in result.matches:
            active_index = result.matches.index(previous_active)
        elif previous_index is not None:
            active_index = min(previous_index, len(result.matches) - 1)
        else:
            active_index = 0
        self._terminal_search_active_index = active_index
        should_scroll = self._terminal_search_should_scroll_on_refresh
        self._terminal_search_should_scroll_on_refresh = False
        self._apply_terminal_search_state(
            scroll_to_active=should_scroll and active_index is not None
        )

    def _navigate_terminal_search(self, direction: int) -> None:
        """Move through search results with deterministic wraparound."""
        if direction not in {-1, 1}:
            raise ValueError("direction must be -1 or 1")
        was_unpositioned = self._terminal_search_active_index is None
        if (
            self._terminal_search_dirty
            or self._terminal_search_timer.isActive()
            or self._terminal_search_max_timer.isActive()
        ):
            self._refresh_terminal_search()

        matches = self._terminal_search_result.matches
        if not matches:
            return
        if was_unpositioned:
            active_index = 0 if direction > 0 else len(matches) - 1
        else:
            current = self._terminal_search_active_index or 0
            active_index = (current + direction) % len(matches)
        self._terminal_search_active_index = active_index
        self._apply_terminal_search_state(scroll_to_active=True)

    def _apply_terminal_search_state(self, *, scroll_to_active: bool) -> None:
        """Synchronize result count, highlights, and optional viewport movement."""
        matches = self._terminal_search_result.matches
        active_index = self._terminal_search_active_index
        self.output_view.set_search_matches(matches, active_index)
        self.terminal_search_bar.set_match_status(
            0 if active_index is None else active_index + 1,
            len(matches),
            truncated=self._terminal_search_result.truncated,
        )
        if scroll_to_active and active_index is not None:
            self.output_view.scroll_to_document_row(matches[active_index].row)
        self._position_terminal_search_bar()

    def _handle_terminal_search_closed(self) -> None:
        """Return keyboard focus after an explicit search dismissal."""
        self._close_terminal_search(restore_focus=True)

    def _close_terminal_search(self, *, restore_focus: bool) -> None:
        """Clear search state, optionally restoring the ordinary input target."""
        self._terminal_search_timer.stop()
        self._terminal_search_max_timer.stop()
        self.terminal_search_bar.close()
        self._terminal_search_result = TerminalSearchResult()
        self._terminal_search_active_index = None
        self._terminal_search_should_scroll_on_refresh = False
        self._terminal_search_dirty = False
        self.output_view.clear_search_matches()
        if restore_focus and self._active_ssh_secret_request_id is None:
            if self.command_input is not None and self.command_input.isEnabled():
                self.command_input.setFocus(Qt.FocusReason.OtherFocusReason)
            else:
                self.output_view.setFocus(Qt.FocusReason.OtherFocusReason)

    def _send_command(self) -> None:
        """Submit one complete line through the temporary pipe fallback."""
        if self.command_input is None or not self.command_insertion_available:
            return
        command = self.command_input.text()
        self.command_input.clear()
        command_line = f"{command}\n"
        self.output_view.scroll_to_bottom()
        if not self.backend.echoes_input:
            self.output_view.feed_text(f"> {command}\r\n")
        self.backend.write(command_line.encode(self.backend.text_encoding, errors="replace"))

    def _send_direct_input(self, text: str) -> None:
        """Encode generated terminal text only while the backend is ready."""
        data = text.encode(self.backend.text_encoding, errors="replace")
        self._send_direct_bytes(data)

    def _send_direct_bytes(self, data: bytes) -> None:
        """Send pre-encoded input only while the backend is still writable."""
        if (
            not self.backend.supports_direct_input
            or self._input_locked
            or not self.output_view.input_enabled
            or self._is_shut_down
            or self._backend_finished
            or not self.backend.is_running
        ):
            return
        self.output_view.scroll_to_bottom()
        self.backend.prepare_input(self.screen.cursor.column, self.screen.columns)
        self.backend.write(data)

    def _handle_paste_request(self, clipboard_text: str) -> None:
        """Sanitize a clipboard snapshot and confirm potentially active input."""
        self._paste_text(clipboard_text, one_line=False)

    def _handle_one_line_paste_request(self, clipboard_text: str) -> None:
        """Send only explicitly requested one-line text, with all ordinary gates."""
        self._paste_text(clipboard_text, one_line=True)

    def _paste_text(self, clipboard_text: str, *, one_line: bool) -> None:
        if not self.command_insertion_available or not self.backend.supports_direct_input:
            return
        backend = self.backend
        try:
            prepared = prepare_paste(
                clipboard_text,
                bracketed=self.screen.bracketed_paste,
                one_line=one_line,
            )
        except PasteTooLargeError as error:
            self._handle_warning(validation_text(str(error)))
            return

        if prepared.requires_confirmation:
            # Preview the actual lossy-encoding result too, not characters that
            # this backend would silently replace on the way to the shell.
            clipboard_text = prepared.plain_text.encode(
                backend.text_encoding, errors="replace"
            ).decode(backend.text_encoding, errors="replace")
            prepared = prepare_paste(
                clipboard_text, bracketed=self.screen.bracketed_paste, one_line=one_line
            )
            reviewed_text = self._confirm_paste(prepared, one_line=one_line)
            if reviewed_text is None:
                return
            if self.backend is not backend or not self.command_insertion_available:
                return

            current_bracketed_mode = self.screen.bracketed_paste
            if "\n" in reviewed_text and prepared.is_bracketed and not current_bracketed_mode:
                self._handle_warning(
                    tr(
                        "Paste cancelled because the terminal disabled bracketed paste while confirmation was open. Try pasting again."
                    )
                )
                return
            try:
                prepared = prepare_paste(
                    reviewed_text,
                    bracketed=current_bracketed_mode,
                    one_line=one_line,
                )
            except PasteTooLargeError as error:
                self._handle_warning(validation_text(str(error)))
                return

        payload = prepared.payload.encode(self.backend.text_encoding, errors="replace")
        if len(payload) > MAX_PASTE_PAYLOAD_BYTES:
            self._handle_warning(
                tr(
                    "Paste cancelled: the encoded clipboard data exceeds the {p0}-byte limit.",
                    p0=f"{MAX_PASTE_PAYLOAD_BYTES:,}",
                )
            )
            return
        self.output_view.clear_selection()
        self._send_direct_bytes(payload)

    def _confirm_paste(self, prepared: PreparedPaste, *, one_line=False) -> str | None:
        """Ask before an unprotected multiline or unusually large paste."""
        dialog = PastePreviewDialog(
            prepared.plain_text,
            unprotected=prepared.has_line_breaks and not prepared.is_bracketed,
            encoding=self.backend.text_encoding,
            one_line=one_line,
            parent=self,
        )
        try:
            return dialog.paste_text if dialog.exec() == QDialog.DialogCode.Accepted else None
        finally:
            schedule_delete_later(dialog)

    def _handle_data(self, data: bytes) -> None:
        """Parse one arbitrary terminal byte chunk and return requested replies."""
        if self._is_shut_down:
            return
        if self._session_logger is not None and self._active_ssh_secret_request_id is None:
            self._session_logger.append(data)
        self._recent_output.extend(data)
        overflow = len(self._recent_output) - RECENT_OUTPUT_LIMIT_BYTES
        if overflow > 0:
            del self._recent_output[:overflow]

        try:
            update = self.output_view.feed(data)
        except TerminalScreenError as error:
            self._show_status(tr("Terminal parser error: {p0}", p0=f"{error}"), is_error=True)
            return

        for reply in update.replies:
            self.backend.write(reply.encode(self.backend.text_encoding, errors="replace"))
        self._apply_window_title(update.window_title)
        if update.bell_requested:
            self._handle_bell_request()

    def _handle_bell_request(self) -> None:
        """Publish a BEL event and optionally show one quiet, bounded frame flash."""
        if self._is_shut_down or self._backend_finished:
            return
        self.bell_requested.emit()
        if not self._visual_bell_enabled:
            return
        if self.visual_bell_active:
            # Keep a hostile BEL stream from repeatedly polishing the style or
            # extending the chrome forever. Raw bell events still reach the
            # workspace, while the visual layer coalesces them into one bounded
            # flash per timer interval.
            return
        self.visual_bell_frame.setProperty("bellState", "active")
        self.visual_bell_frame.style().unpolish(self.visual_bell_frame)
        self.visual_bell_frame.style().polish(self.visual_bell_frame)
        self._position_visual_bell_frame()
        self.visual_bell_frame.show()
        self._visual_bell_timer.start()

    def _clear_visual_bell_flash(self) -> None:
        """Stop and hide visual BEL chrome immediately and idempotently."""
        self._visual_bell_timer.stop()
        self.visual_bell_frame.hide()
        if self.visual_bell_frame.property("bellState") != "inactive":
            self.visual_bell_frame.setProperty("bellState", "inactive")
            self.visual_bell_frame.style().unpolish(self.visual_bell_frame)
            self.visual_bell_frame.style().polish(self.visual_bell_frame)

    def _position_visual_bell_frame(self) -> None:
        """Cover the viewport without participating in layout or PTY sizing."""
        self.visual_bell_frame.setGeometry(self.output_view.viewport().rect())

    def _may_restore_async_input_focus(self) -> bool:
        """Do not redirect input after the user selects another application field."""
        focus = QApplication.focusWidget()
        return (
            focus is None
            or focus is self
            or self.isAncestorOf(focus)
            or (self._waiting_for_started and focus is self._starting_focus_widget)
        )

    def _handle_started(self) -> None:
        """Enable the appropriate input UI once the child is ready."""
        if self._is_shut_down:
            return
        restore_focus = self._may_restore_async_input_focus()
        self._waiting_for_started = False
        self._starting_focus_widget = None
        process_id = self.backend.process_id
        if process_id is not None:
            self.process_started.emit(process_id)
        self._hide_status()
        self._enable_regular_input(restore_focus=restore_focus)

    def _handle_finished(self, exit_code: int) -> None:
        """Disable input and show the backend-neutral session result."""
        if self._backend_finished:
            return
        restore_focus = self._may_restore_async_input_focus()
        self._backend_finished = True
        self.stop_session_logging()
        self._waiting_for_started = False
        self._starting_focus_widget = None
        self._exit_code = exit_code
        self.connection_advisory.hide()
        self._clear_visual_bell_flash()
        self._disable_input()
        is_error = False
        try:
            update = self.output_view.finish()
        except TerminalScreenError as error:
            message = tr("Terminal parser error: {p0}", p0=f"{error}")
            is_error = True
        else:
            self._apply_window_title(update.window_title)
            if self._last_backend_error is not None:
                message = tr(
                    "{p0} (session result {p1})",
                    p0=f"{self._last_backend_error}",
                    p1=f"{exit_code}",
                )
                is_error = True
            else:
                message = tr("Session ended with code {p0}.", p0=f"{exit_code}")
        self._show_status(message, is_error=is_error)
        self._show_session_ended(message, is_error=is_error)
        if restore_focus and not self._is_shut_down and self.isVisible() and self._focus_guard():
            self.output_view.setFocus(Qt.FocusReason.OtherFocusReason)
        if not self._is_shut_down:
            self.process_finished.emit(exit_code)
        if self._is_shut_down:
            self._emit_ready_to_delete()

    def _handle_error(self, message: str) -> None:
        """Display backend errors in the terminal output area."""
        if self._is_shut_down:
            return
        self._last_backend_error = message
        self._disable_input()
        self._show_status(message, is_error=True)

    def _handle_warning(self, message: str) -> None:
        """Display a recoverable problem without disabling a live terminal."""
        if self._is_shut_down or self._backend_finished:
            return
        self._show_status(message, is_warning=True)

    def _disable_input(self) -> None:
        """Stop both direct input and the compatibility line editor."""
        self.ssh_secret_prompt.cancel()
        self._active_ssh_secret_request_id = None
        self._active_ssh_secret_is_host_password = None
        self.ssh_secret_prompt.finish()
        self.ssh_prompt_overlay.hide()
        if not self._is_shut_down:
            self.find_terminal_shortcut.setEnabled(True)
            self.output_view.set_find_enabled(True)
        self._disable_regular_input()

    def _disable_regular_input(self) -> None:
        """Pause child-directed input while retaining an inline secret request."""
        self.output_view.set_input_enabled(False)
        if self.command_input is not None:
            self.command_input.setEnabled(False)

    def _enable_regular_input(self, *, restore_focus: bool = True) -> None:
        """Restore child-directed input only after every secret request is gone."""
        if (
            self._active_ssh_secret_request_id is not None
            or self._input_locked
            or self._is_shut_down
            or self._backend_finished
            or not self.backend.is_running
        ):
            return
        if self.backend.supports_direct_input:
            self.output_view.set_input_enabled(True)
            if restore_focus and self.isVisible() and self._focus_guard():
                self.output_view.setFocus()
        elif self.command_input is not None:
            self.command_input.setEnabled(True)
            if restore_focus and self.isVisible() and self._focus_guard():
                self.command_input.setFocus()

    def _forward_ssh_secret(self, secret: str) -> None:
        """Publish a cleared widget response with its exact opaque request ID."""
        request_id = self._active_ssh_secret_request_id
        if request_id is None:
            return
        try:
            self.ssh_secret_submitted.emit(request_id, secret)
        finally:
            secret = ""

    def _forward_ssh_secret_cancel(self) -> None:
        """Publish cancellation for only the request currently shown inline."""
        request_id = self._active_ssh_secret_request_id
        if request_id is not None:
            self.ssh_secret_cancel_requested.emit(request_id)

    def _show_status(
        self,
        message: str,
        *,
        is_error: bool = False,
        is_warning: bool = False,
    ) -> None:
        """Show application state outside the child-owned terminal grid."""
        status_type = "error" if is_error else "warning" if is_warning else "info"
        self.status_label.setProperty("statusType", status_type)
        self.status_label.setText(message)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.status_label.show()

    def _hide_status(self) -> None:
        """Hide transient application state after successful startup."""
        self.status_label.hide()

    def _show_session_ended(self, message: str, *, is_error: bool) -> None:
        """Replace transient status chrome with an actionable, non-modal end state."""
        self._session_end_action_pending = False
        self.session_end_banner.setProperty("statusType", "error" if is_error else "info")
        self.session_end_title.setText(tr("Connection ended") if is_error else tr("Session ended"))
        self.session_end_detail.setText(message)
        self.reconnect_button.setText(tr("Reconnect"))
        self.reconnect_button.setEnabled(self._reconnect_available)
        self.ended_close_button.setEnabled(True)
        for shortcut in self._ended_shortcuts:
            shortcut.setEnabled(not self._is_shut_down)
        self.status_label.hide()
        self.session_end_banner.style().unpolish(self.session_end_banner)
        self.session_end_banner.style().polish(self.session_end_banner)
        self.session_end_banner.show()
        self._update_session_end_layout()

    def _ended_action(self, action: str) -> None:
        if (
            not self._backend_finished or self._is_shut_down
            or self._session_end_action_pending or not self._focus_guard()
        ):
            return
        if action == "reconnect":
            self._request_reconnect()
        elif action == "close":
            self._request_ended_close()
        elif action == "save":
            self._request_output_export()

    def _request_reconnect(self) -> None:
        """Publish one guarded reconnect intent; the owner creates a fresh session."""
        if (
            not self._reconnect_available
            or not self._backend_finished
            or self._is_shut_down
            or self._session_end_action_pending
        ):
            return
        self._session_end_action_pending = True
        self.reconnect_button.setText(tr("Reconnecting…"))
        self.reconnect_button.setEnabled(False)
        self.ended_close_button.setEnabled(False)
        self.reconnect_requested.emit()

    def _request_ended_close(self) -> None:
        """Ask the owning workspace to close only this already-ended page."""
        if not self._backend_finished or self._is_shut_down or self._session_end_action_pending:
            return
        self._session_end_action_pending = True
        self.reconnect_button.setEnabled(False)
        self.ended_close_button.setEnabled(False)
        self.ended_close_requested.emit()

    def _update_session_end_layout(self) -> None:
        """Stack banner copy and actions when font-driven hints no longer fit."""
        if self.session_end_banner.isHidden():
            return
        margins = self._session_end_layout.contentsMargins()
        available_width = max(0, self.width() - margins.left() - margins.right())
        copy_width = max(
            self.session_end_title.sizeHint().width(),
            self.session_end_detail.sizeHint().width(),
        )
        action_width = (
            sum(
                button.sizeHint().width()
                for button in (self.reconnect_button, self.ended_save_button, self.ended_close_button)
                if not button.isHidden()
            )
            + 2 * self._session_end_actions.spacing()
        )
        direction = (
            QBoxLayout.Direction.TopToBottom
            if copy_width + action_width + self._session_end_layout.spacing() > available_width
            else QBoxLayout.Direction.LeftToRight
        )
        if self._session_end_layout.direction() != direction:
            self._session_end_layout.setDirection(direction)
            self.session_end_banner.updateGeometry()

    def _apply_window_title(self, window_title: str | None) -> None:
        """Publish a sanitized display title while preserving the session name."""
        if window_title is None or self._local_directory_title:
            return
        dynamic_title = sanitize_terminal_title(window_title)
        if self._fixed_connection_title:
            if self.remote_window_title != dynamic_title:
                self.remote_window_title = dynamic_title
                self.display_title_changed.emit(self.display_title)
            return
        if not dynamic_title or dynamic_title == self.terminal_name:
            display_title = self.terminal_name
        else:
            display_title = f"{self.terminal_name} — {dynamic_title}"
        if display_title == self._display_title:
            return
        self._display_title = display_title
        if self._custom_tab_title is None:
            self.display_title_changed.emit(display_title)

    def configure_session_title(
        self, *, local: bool = False, ssh: bool = False, connection: bool = False,
        local_start_directory: str | None = None,
    ) -> None:
        """Select the source of automatic titles without changing session identity."""
        self._fixed_connection_title = ssh or connection
        self._local_directory_title = local
        if local:
            # Set the actual launch folder before addTab(), not
            # after the first asynchronous OS cwd result, to avoid Local N flash.
            self._display_title = sanitize_terminal_title(local_start_directory or str(Path.home()))
            self._directory_timer.start()
            self._poll_local_directory()

    def _poll_local_directory(self) -> None:
        if self._is_shut_down or self._backend_finished:
            self._directory_timer.stop()
            self._directory_future = None
            return
        pid = self.backend.process_id
        if self._directory_future is not None:
            if not self._directory_future.done():
                return
            future, self._directory_future = self._directory_future, None
            if pid == self._directory_pid:
                directory = future.result()
                if directory and (title := sanitize_terminal_title(directory)):
                    if title != self._display_title:
                        self._display_title = title
                        if self._custom_tab_title is None:
                            self.display_title_changed.emit(title)
        if pid is not None and self.backend.is_running:
            self._directory_pid = pid
            self._directory_future = DIRECTORY_READERS.submit(process_directory, pid)

    def _emit_ready_to_delete(self) -> None:
        """Tell the owner once that asynchronous cleanup is complete."""
        if self._ready_to_delete_emitted:
            return
        if self._session_logger is not None and self._session_logger.busy:
            return
        self._ready_to_delete_emitted = True
        self.ready_to_delete.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Also stop the backend when a page is closed outside its workspace."""
        self.shutdown()
        super().closeEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the terminal end-state actions readable at narrow widths."""
        super().resizeEvent(event)
        self._update_session_end_layout()

    def showEvent(self, event: QShowEvent) -> None:
        """Restore pending input focus only in the owner's active page."""
        super().showEvent(event)
        if (
            self._active_ssh_secret_request_id is not None
            and self.ssh_secret_prompt.secret_input.isEnabled()
        ):
            if self._focus_guard():
                self.ssh_secret_prompt.secret_input.setFocus(Qt.FocusReason.OtherFocusReason)
        elif not self.terminal_search_bar.isHidden():
            self._position_terminal_search_bar()
            if self._focus_guard():
                self.terminal_search_bar.query_input.setFocus(Qt.FocusReason.OtherFocusReason)
            if self._terminal_search_dirty:
                self._schedule_terminal_search_refresh(
                    ScreenUpdate(frozenset()),
                    False,
                )

    def _send_terminal_size(self, columns: int, rows: int) -> None:
        """Keep the backend in lockstep with a committed screen-grid size."""
        if self._last_backend_size == (columns, rows):
            return
        self._last_backend_size = (columns, rows)
        self.backend.resize(columns, rows)
