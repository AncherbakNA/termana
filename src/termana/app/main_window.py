"""Main window for the Termana application."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from termana.app import window_actions as app_window_actions
from termana.app import window_close as app_window_close
from termana.app import window_preferences as app_window_preferences
from termana.app import window_update_sync as app_window_update_sync
from termana.app.paths import application_config_directory
from termana.connections import window_catalog as connections_window_catalog
from termana.connections import window_transfer as connections_window_transfer
from termana.ssh import window_accounts as ssh_window_accounts
from termana.ssh import window_authentication as ssh_window_authentication
from termana.ssh import window_launch as ssh_window_launch
from termana.ssh import window_login_resolution as ssh_window_login_resolution
from termana.telnet import window_launch as telnet_window_launch
from termana.terminal import window_lifecycle as terminal_window_lifecycle
from termana.ui.i18n import tr

if TYPE_CHECKING:
    from termana.ssh.embedded_ssh_backend import EmbeddedSshBackend
    from termana.ssh.embedded_ssh_session import EmbeddedSshAuthenticationSession

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QProgressBar, QSplitter

from termana import APP_NAME
from termana.app.background_operation import BackgroundOperation
from termana.app.window_contracts import (
    CONFIRMATION_MESSAGE_TIMEOUT_MS,
    DEFAULT_SIDEBAR_WIDTH,
    DEFAULT_WINDOW_SIZE,
    LOGGER,
    MINIMUM_WINDOW_SIZE,
    WINDOW_STATE_VERSION,
    BackgroundOperationStarter,
    CredentialOperationStarter,
    SshAuthenticationFactory,
    _ConnectionOpenState,
    _ConnectionTransferKind,
    _ConnectionTransferOperationContext,
    _PendingConnectionImportCommit,
    _SshConnectOrigin,
    _SshCredentialRemovalContext,
    _SshLoginLookupResult,
    _SshLoginResolution,
    _SshLoginResolutionOperation,
    _SshLoginResolutionRequest,
    _SshPreferenceOperationContext,
    _SshPreferenceRequest,
    _start_background_operation,
    _start_credential_operation,
)
from termana.app.window_layout_controller import WindowLayoutController
from termana.app.workspace_sidebar import WorkspaceSidebar
from termana.connections.connection_profile_editor import ConnectionProfileEditor
from termana.connections.widgets.connections_sidebar import ConnectionsSidebar
from termana.models.connection import ConnectionCatalog, ConnectionFolder, ConnectionProfile
from termana.models.settings import (
    ApplicationSettings,
    SshEngine,
    TerminalFontSettings,
    ThemeId,
)
from termana.models.ssh_jump import SshJumpMode
from termana.sftp.sftp_controller import SftpController
from termana.ssh.dialogs.saved_ssh_logins_dialog import SavedSshLoginsDialog
from termana.ssh.dialogs.ssh_login_dialog import SshLoginDialog
from termana.ssh.dialogs.ssh_tunnel_launch_dialog import SshTunnelLaunchDialog
from termana.ssh.host_key_approval import SshHostKeyApproval
from termana.ssh.known_hosts import KnownHostsUpdater, SshHostKeyMismatch
from termana.ssh.repository import SshCredentialRepository
from termana.ssh.ssh_auth import (
    SshAuthenticationSession,
    SshCredentialMutationCoordinator,
    create_ssh_authentication_session,
)
from termana.ssh.ssh_command import SSH_PROGRAM
from termana.ssh.ssh_config_controller import SshConfigController
from termana.ssh.ssh_group_input_controller import SshGroupInputController
from termana.ssh.ssh_secret_prompt_controller import SshSecretPromptController
from termana.ssh.ssh_session_details_controller import SshSessionDetailsController
from termana.ssh.ssh_session_save_controller import SshSessionSaveController
from termana.storage.command_snippet_store import CommandSnippetStore
from termana.storage.connection_store import ConnectionStore
from termana.storage.connection_transfer import ConnectionTransferStats, DecodedConnectionTransfer
from termana.storage.telnet_warning_store import TelnetWarningStore
from termana.telnet.telnet_backend import TelnetBackendFactory, create_telnet_backend
from termana.terminal.backend_factory import TerminalBackendFactory
from termana.terminal.command_snippet_controller import CommandSnippetController
from termana.terminal.open_terminals_controller import OpenTerminalsController
from termana.terminal.terminal_multi_search_controller import TerminalMultiSearchController
from termana.terminal.terminal_output_controller import TerminalOutputController
from termana.terminal.terminal_zoom_controller import TerminalZoomController
from termana.terminal.widgets.terminal_workspace import TerminalWorkspace
from termana.ui.dialogs.settings_dialog import SettingsDialog
from termana.ui.initial_window_fit import InitialWindowFit
from termana.ui.settings_controller import SettingsController
from termana.ui.theme import ThemeColors
from termana.ui.ui_actions import create_window_action
from termana.ui.widgets.readable_status_bar import ReadableStatusBar


class MainWindow(QMainWindow):
    """Coordinate Termana's menus and main interface components."""

    def __init__(
        self,
        connection_store: ConnectionStore | None = None,
        *,
        terminal_backend_factory: TerminalBackendFactory | None = None,
        telnet_backend_factory: TelnetBackendFactory | None = None,
        known_hosts_updater: KnownHostsUpdater | None = None,
        credential_repository: SshCredentialRepository | None = None,
        ssh_program: str | None = SSH_PROGRAM,
        embedded_ssh_known_hosts: Path | None = None,
        embedded_ssh_agent_path: Path | None = None,
        ssh_authentication_factory: SshAuthenticationFactory = (create_ssh_authentication_session),
        credential_operation_starter: CredentialOperationStarter | None = None,
        connection_transfer_operation_starter: BackgroundOperationStarter | None = None,
        settings_controller: SettingsController | None = None,
        settings_load_error: str | None = None,
        command_snippet_store: CommandSnippetStore | None = None,
        system_ssh_config_store=None,
        rdp_identity_repository=None,
    ) -> None:
        """Initialize the main window."""
        super().__init__()
        if embedded_ssh_known_hosts is not None and (
            not isinstance(embedded_ssh_known_hosts, Path)
            or not embedded_ssh_known_hosts.is_absolute()
        ):
            raise ValueError("Embedded SSH requires an absolute known_hosts path")
        self._embedded_ssh_known_hosts = embedded_ssh_known_hosts
        self._system_ssh_config_store = system_ssh_config_store
        self._rdp_identity_repository = rdp_identity_repository
        if embedded_ssh_agent_path is not None and (
            embedded_ssh_known_hosts is None
            or not isinstance(embedded_ssh_agent_path, Path)
            or not embedded_ssh_agent_path.is_absolute()
        ):
            raise ValueError("Embedded SSH agent requires an explicit absolute path and engine")
        self._embedded_ssh_agent_path = embedded_ssh_agent_path

        self.setWindowTitle(APP_NAME)
        if embedded_ssh_known_hosts is not None:
            self.setWindowTitle(tr("{p0} · Embedded SSH", p0=f"{APP_NAME}"))
        self.resize(*DEFAULT_WINDOW_SIZE)
        self.setMinimumSize(*MINIMUM_WINDOW_SIZE)

        status_bar = ReadableStatusBar()
        status_bar.setObjectName("applicationStatusBar")
        status_bar.setSizeGripEnabled(False)
        status_bar.showMessage(tr("Ready"))
        self.setStatusBar(status_bar)
        if self._rdp_identity_repository is not None:
            self._rdp_identity_repository.message.connect(status_bar.showMessage)
            self._rdp_identity_repository.pending_count_changed.connect(
                self._rdp_pending_count_changed
            )
        self._ssh_login_progress = QProgressBar(status_bar)
        self._ssh_login_progress.setObjectName("sshLoginResolutionProgress")
        self._ssh_login_progress.setAccessibleName(tr("Checking saved SSH logins"))
        self._ssh_login_progress.setRange(0, 0)
        self._ssh_login_progress.setTextVisible(False)
        self._ssh_login_progress.setFixedWidth(72)
        self._ssh_login_progress.hide()
        status_bar.addPermanentWidget(self._ssh_login_progress)
        self._connection_transfer_progress = QProgressBar(status_bar)
        self._connection_transfer_progress.setObjectName("connectionTransferProgress")
        self._connection_transfer_progress.setAccessibleName(tr("Transferring connections"))
        self._connection_transfer_progress.setRange(0, 0)
        self._connection_transfer_progress.setTextVisible(False)
        self._connection_transfer_progress.setFixedWidth(72)
        self._connection_transfer_progress.hide()
        status_bar.addPermanentWidget(self._connection_transfer_progress)

        application = QApplication.instance()
        if not isinstance(application, QApplication):
            raise RuntimeError("MainWindow requires a QApplication")
        self._settings_controller = settings_controller or SettingsController(
            application,
            parent=self,
        )

        self.connections_sidebar = ConnectionsSidebar()
        self.workspace_sidebar = WorkspaceSidebar(self.connections_sidebar)
        self.terminal_workspace = TerminalWorkspace(
            terminal_backend_factory,
            terminal_colors=self._settings_controller.colors.terminal,
            terminal_font=self._settings_controller.settings.terminal_font,
            terminal_scrollback_lines=(
                self._settings_controller.settings.terminal_scrollback_lines
            ),
            visual_bell_enabled=self._settings_controller.settings.visual_bell_enabled,
            confirm_session_close=self._settings_controller.settings.confirm_session_close,
        )
        self._known_hosts_updater = known_hosts_updater or KnownHostsUpdater(self)
        self.terminal_workspace.set_interface_colors(self._settings_controller.colors)
        self.terminal_workspace.set_local_terminal_settings(
            self._settings_controller.settings.local_terminal
        )
        self.terminal_workspace.set_backspace_sends_ctrl_h(
            self._settings_controller.settings.backspace_sends_ctrl_h
        )
        self.terminal_workspace.set_highlighting(
            self._settings_controller.settings.terminal_highlighting
        )
        self._command_snippet_controller = CommandSnippetController(
            self, self.terminal_workspace, command_snippet_store
        )
        self._command_snippet_controller.message.connect(self.statusBar().showMessage)
        self._command_snippet_controller.library_changed.connect(
            self.workspace_sidebar.commands.set_snippets
        )
        self.workspace_sidebar.commands_requested.connect(self._show_commands_sidebar)
        self.workspace_sidebar.commands.manage_requested.connect(
            lambda snippet_id: self._command_snippet_controller.open_dialog(
                select_snippet_id=snippet_id
            )
        )
        self.workspace_sidebar.commands.insert_requested.connect(
            self._command_snippet_controller.insert_from_sidebar
        )
        self.terminal_workspace.open_sessions_changed.connect(self._update_commands_sidebar_target)
        self._open_terminals_controller = OpenTerminalsController(self, self.terminal_workspace)
        self._terminal_multi_search_controller = TerminalMultiSearchController(
            self, self.terminal_workspace
        )
        self._terminal_multi_search_controller.message.connect(self.statusBar().showMessage)
        self._ssh_session_details_controller = SshSessionDetailsController(
            self, self.terminal_workspace
        )
        self._sftp_controller = SftpController(
            self,
            self.terminal_workspace,
            self.workspace_sidebar,
            auto_open=self._settings_controller.settings.sftp_auto_open,
        )
        self._open_terminals_controller.message.connect(self.statusBar().showMessage)
        self._ssh_session_save_controller = SshSessionSaveController(
            self,
            self.terminal_workspace,
            catalog=lambda: self._connection_catalog,
            available=self._ssh_session_save_available,
            scope=lambda: self._connection_store,
            ssh_engine=lambda: (
                SshEngine.EMBEDDED
                if self._embedded_ssh_known_hosts is not None
                else SshEngine.SYSTEM
            ),
            persist=lambda profile, folder: self._add_connection_profile(
                profile, parent_folder_id=folder
            ),
        )
        self._ssh_session_save_controller.message.connect(self.statusBar().showMessage)
        self._connection_profile_editor = ConnectionProfileEditor(
            self,
            catalog=lambda: self._connection_catalog,
            available=self._ssh_session_save_available,
            scope=lambda: self._connection_store,
            commit=lambda candidate, selected: self._commit_connection_catalog(
                candidate, select_item_id=selected
            ),
            new_route=lambda: (
                SshJumpMode.DIRECT
                if self._embedded_ssh_known_hosts is not None
                else SshJumpMode.CONFIG
            ),
            accounts=self._ssh_account_choices,
            manage_accounts=self._manage_ssh_accounts,
            ssh_engine=lambda: (
                SshEngine.EMBEDDED
                if self._embedded_ssh_known_hosts is not None
                else SshEngine.SYSTEM
            ),
        )
        self._connection_profile_editor.message.connect(self.statusBar().showMessage)
        self.terminal_workspace.saved_commands_requested.connect(
            self._command_snippet_controller.open_dialog
        )
        self._terminal_output_controller = TerminalOutputController(self, self.terminal_workspace)
        from termana.terminal.session_logging_controller import SessionLoggingController

        self._session_logging_controller = SessionLoggingController(self, self.terminal_workspace)
        self.session_logging_action = self._session_logging_controller.action
        self.open_session_logs_action = self._session_logging_controller.open_action
        self.terminal_workspace.output_export_requested.connect(
            self._terminal_output_controller.request_export
        )
        self._terminal_output_controller.message.connect(self.statusBar().showMessage)
        self._terminal_output_controller.finished.connect(
            self._finish_close_after_terminal_shutdown
        )
        self._credential_repository = credential_repository
        self._ssh_account_dialog = None
        if credential_operation_starter is not None and not callable(credential_operation_starter):
            raise ValueError("credential_operation_starter must be callable")
        self._credential_operation_starter = (
            _start_credential_operation
            if credential_operation_starter is None
            else credential_operation_starter
        )
        if connection_transfer_operation_starter is not None and not callable(
            connection_transfer_operation_starter
        ):
            raise ValueError("connection_transfer_operation_starter must be callable")
        self._connection_transfer_operation_starter = (
            _start_background_operation
            if connection_transfer_operation_starter is None
            else connection_transfer_operation_starter
        )
        self._ssh_credential_mutation_coordinator = SshCredentialMutationCoordinator(
            self._credential_operation_starter,
            parent=self,
        )
        self._ssh_credential_mutation_coordinator.pending_count_changed.connect(
            self._handle_ssh_credential_mutation_pending_count_changed
        )
        self._ssh_login_resolution_operations: dict[
            UUID,
            _SshLoginResolutionOperation,
        ] = {}
        self._active_ssh_login_resolution_id: UUID | None = None
        self._ssh_login_dialogs: dict[UUID, SshLoginDialog] = {}
        self._ssh_host_key_dialogs: set[QDialog] = set()
        self._ssh_preference_operations: dict[
            UUID,
            tuple[BackgroundOperation, _SshPreferenceOperationContext],
        ] = {}
        self._ssh_credential_removal_operations: dict[
            UUID,
            tuple[BackgroundOperation, _SshCredentialRemovalContext],
        ] = {}
        if ssh_program is not None and (not isinstance(ssh_program, str) or not ssh_program):
            raise ValueError("ssh_program must be non-empty text or None")
        self._ssh_program = ssh_program
        self._ssh_authentication_factory = ssh_authentication_factory
        self._ssh_authentication_sessions: dict[
            UUID, SshAuthenticationSession | EmbeddedSshAuthenticationSession
        ] = {}
        self._ssh_authentication_terminal_ids: dict[UUID, UUID] = {}
        self._pending_ssh_authentication_cancellations: set[UUID] = set()
        self._ssh_tunnel_confirmation_active = False
        self._ssh_tunnel_launch_dialog: SshTunnelLaunchDialog | None = None
        self._telnet_backend_factory = (
            telnet_backend_factory if telnet_backend_factory is not None else create_telnet_backend
        )
        self._close_waiting_for_terminals = False
        self._application_close_confirmed = False
        self._terminal_shutdown_complete = False
        self._private_recovery_dialog: QDialog | None = None
        self._connection_store = connection_store
        catalog_path = getattr(connection_store, "path", None)
        warning_directory = (
            catalog_path.parent
            if isinstance(catalog_path, Path)
            else application_config_directory()
        )
        self._telnet_warning_store = TelnetWarningStore(
            warning_directory / "telnet-warning-choices.json"
        )
        self._connection_store_ready = True
        self._connection_catalog = ConnectionCatalog()
        self._connection_transfer_operation: _ConnectionTransferOperationContext | None = None
        self._connection_transfer_confirmation_active = False
        self._connection_transfer_dialog: QDialog | None = None
        self._ssh_secret_prompts = SshSecretPromptController(
            self.terminal_workspace,
            authentication_for=self._ssh_authentication_sessions.get,
            terminal_for=self._ssh_authentication_terminal_ids.get,
            is_closing=lambda: self._close_waiting_for_terminals,
        )
        self._terminal_zoom_controller = TerminalZoomController(
            self._settings_controller,
            is_closing=lambda: self._close_waiting_for_terminals,
            show_message=self.statusBar().showMessage,
            confirmation_timeout_ms=CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("mainSplitter")
        self.main_splitter.setHandleWidth(5)
        self.main_splitter.addWidget(self.workspace_sidebar)
        self.main_splitter.addWidget(self.terminal_workspace)
        workspace_width = DEFAULT_WINDOW_SIZE[0] - DEFAULT_SIDEBAR_WIDTH
        self.main_splitter.setSizes([DEFAULT_SIDEBAR_WIDTH, workspace_width])
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.main_splitter)

        self._create_actions()
        self._window_layout_controller = WindowLayoutController(
            self,
            self.main_splitter,
            self.toggle_sidebar_action,
            self.terminal_workspace,
            self._settings_controller,
            default_window_size=DEFAULT_WINDOW_SIZE,
            default_sidebar_width=DEFAULT_SIDEBAR_WIDTH,
            state_version=WINDOW_STATE_VERSION,
            warn=LOGGER.warning,
        )
        self._initial_window_fit = InitialWindowFit(
            self, allowed=lambda: self._window_layout_controller.needs_initial_fit
        )
        self._ssh_config_controller = SshConfigController(
            self,
            self._open_connection,
            available=lambda: self._embedded_ssh_known_hosts is not None,
        )
        self.open_ssh_config_action = create_window_action(
            self,
            tr("Open from SSH config…"),
            "openSupportedSshConfigAction",
            self._ssh_config_controller.open,
        )
        self.open_ssh_config_action.setEnabled(self._embedded_ssh_known_hosts is not None)
        from termana.ssh.ssh_gateway_import_controller import SshGatewayImportController

        self._gateway_import_controller = SshGatewayImportController(
            self,
            self._system_ssh_config_store,
            available=lambda: (
                self._embedded_ssh_known_hosts is None and self._system_ssh_config_store is not None
            ),
        )
        self.import_gateway_settings_action = create_window_action(
            self,
            tr("Import gateway SSH settings…"),
            "importGatewaySshSettingsAction",
            self._gateway_import_controller.open,
        )
        self.import_gateway_settings_action.setEnabled(
            self._embedded_ssh_known_hosts is None and self._system_ssh_config_store is not None
        )
        self.group_input_action = QAction(tr("Group keyboard input…"), self)
        self.group_input_action.setObjectName("sshGroupInputAction")
        self.group_input_action.setStatusTip(
            tr("Send keyboard keys to explicitly selected ready SSH terminals")
        )
        self._group_input_controller = SshGroupInputController(
            self, self.terminal_workspace, self.group_input_action
        )
        self._create_toolbar()
        self._sftp_controller.set_interface_colors(
            self._settings_controller.colors,
            monochrome=self._settings_controller.displayed_theme
            in (ThemeId.MONOCHROME, ThemeId.MONOCHROME_LIGHT),
        )
        self._create_menu_bar()
        self._connect_component_signals()
        self._load_connection_catalog()
        self._update_connection_transfer_actions()
        self._restore_window_layout()
        if settings_load_error is not None:
            previous_message = self.statusBar().currentMessage()
            warning = tr("Could not load settings: {p0}", p0=f"{settings_load_error}")
            if previous_message and previous_message != "Ready":
                warning = f"{previous_message} | {warning}"
            self.statusBar().showMessage(warning)

    @property
    def connection_catalog(self) -> ConnectionCatalog:
        """Return the currently persisted saved-connections snapshot."""
        return self._connection_catalog

    def _create_actions(self) -> None:
        """Create commands that can be shared by menus and toolbars."""
        return app_window_actions._create_actions(self)

    def _create_menu_bar(self) -> None:
        """Create the application menu hierarchy."""
        return app_window_actions._create_menu_bar(self)

    def _choose_terminal_grid(self, _checked: bool = False) -> None:
        return app_window_actions._choose_terminal_grid(self, _checked)

    def _create_toolbar(self) -> None:
        """Present real shared commands as labeled workbench tools, never placeholders."""
        return app_window_actions._create_toolbar(self)

    def _apply_toolbar_actions(self, selected: tuple[str, ...]) -> None:
        """Remove only toolbar presentations, never hide the shared menu QAction."""
        return app_window_actions._apply_toolbar_actions(self, selected)

    def _refresh_action_icons(self, colors: ThemeColors) -> None:
        """Keep toolbar and menu glyphs legible when the live theme changes."""
        return app_window_actions._refresh_action_icons(self, colors)

    def _connect_component_signals(self) -> None:
        """Connect independent interface components to application commands."""
        self.connections_sidebar.new_connection_requested.connect(
            self._open_new_connection_in_folder
        )
        self.connections_sidebar.new_folder_requested.connect(
            self._open_new_connection_folder_in_parent
        )
        self.connections_sidebar.rename_folder_requested.connect(self._rename_connection_folder)
        self.connections_sidebar.delete_folder_requested.connect(self._delete_connection_folder)
        self.connections_sidebar.move_item_requested.connect(self._move_connection_catalog_item)
        self.connections_sidebar.retry_catalog_requested.connect(self._load_connection_catalog)
        self.connections_sidebar.connection_activated.connect(self._open_connection)
        self.connections_sidebar.edit_connection_requested.connect(self._edit_connection_profile)
        self.connections_sidebar.duplicate_connection_requested.connect(
            self._duplicate_connection_profile
        )
        self.connections_sidebar.favorite_connection_requested.connect(
            self._set_connection_favorite
        )
        self.connections_sidebar.export_item_requested.connect(
            self._open_connection_item_export_dialog
        )
        self.connections_sidebar.delete_connection_requested.connect(
            self._delete_connection_profile
        )
        self.terminal_workspace.new_connection_requested.connect(self._open_new_connection_dialog)
        self.terminal_workspace.local_terminal_requested.connect(self._open_local_terminal)
        self.terminal_workspace.local_terminal_opened.connect(self._show_local_terminal_opened)
        self.terminal_workspace.terminal_closed.connect(self._show_terminal_closed)
        self.terminal_workspace.terminal_count_changed.connect(self._update_terminal_tab_actions)
        self.terminal_workspace.active_terminal_changed.connect(self._update_terminal_tab_actions)
        self.terminal_workspace.pane_layout_changed.connect(self._update_terminal_tab_actions)
        self.terminal_workspace.command_terminal_finished.connect(
            self._handle_command_terminal_finished
        )
        self.terminal_workspace.command_terminal_started.connect(
            self._handle_command_terminal_started
        )
        self.terminal_workspace.command_terminal_closing.connect(
            self._handle_command_terminal_closing
        )
        self.terminal_workspace.connection_reconnect_requested.connect(
            self._handle_connection_reconnect_requested
        )
        self.terminal_workspace.ssh_login_submitted.connect(self._handle_ssh_login_submitted)
        self.terminal_workspace.ssh_secret_submitted.connect(
            self._handle_inline_ssh_secret_submitted
        )
        self.terminal_workspace.ssh_secret_cancel_requested.connect(
            self._handle_inline_ssh_secret_cancel_requested
        )
        self.terminal_workspace.terminal_zoom_requested.connect(
            self._terminal_zoom_controller.handle_request
        )
        self.terminal_workspace.all_terminals_stopped.connect(
            self._finish_close_after_terminal_shutdown
        )
        self._known_hosts_updater.removal_finished.connect(
            self._handle_known_hosts_removal_finished
        )
        self._settings_controller.theme_changed.connect(self._apply_theme_colors)
        self._settings_controller.terminal_font_changed.connect(self._apply_terminal_font)
        self._settings_controller.local_terminal_changed.connect(
            self.terminal_workspace.set_local_terminal_settings
        )
        self._settings_controller.backspace_sends_ctrl_h_changed.connect(
            self.terminal_workspace.set_backspace_sends_ctrl_h
        )
        self._settings_controller.toolbar_actions_changed.connect(self._apply_toolbar_actions)
        self.terminal_workspace.local_terminal_error.connect(self.statusBar().showMessage)
        self._settings_controller.terminal_highlighting_changed.connect(
            self.terminal_workspace.set_highlighting
        )
        self._settings_controller.confirm_session_close_changed.connect(
            self.terminal_workspace.set_confirm_session_close
        )
        self._settings_controller.terminal_scrollback_lines_changed.connect(
            self._apply_terminal_scrollback_lines
        )
        self._settings_controller.visual_bell_enabled_changed.connect(
            self._apply_visual_bell_enabled
        )
        self._settings_controller.sftp_auto_open_changed.connect(
            self._sftp_controller.set_auto_open
        )
        self._update_terminal_tab_actions()

    def _update_terminal_tab_actions(self, _tab_index: int = -1) -> None:
        """Enable tab-only actions only when they can affect a terminal."""
        self._update_commands_sidebar_target()
        self.rename_terminal_tab_action.setEnabled(
            self.terminal_workspace.current_tab_can_be_renamed
        )
        active_pane_count = self.terminal_workspace.active_pane_terminal_count
        self.find_terminal_action.setEnabled(
            self.terminal_workspace.current_terminal_search_available
        )
        self.switch_terminal_action.setEnabled(self.terminal_workspace.terminal_count > 0)
        self.find_open_terminals_action.setEnabled(self.terminal_workspace.terminal_count > 0)
        self.remote_files_action.setEnabled(
            active_pane_count > 0 and not self.terminal_workspace.current_is_rdp
        )
        self.ssh_session_details_action.setEnabled(
            self.terminal_workspace.current_ssh_session_id is not None
        )
        self.save_ssh_session_action.setEnabled(
            self.terminal_workspace.current_ssh_session_id is not None
            and self._ssh_session_save_available()
        )
        self.close_current_terminal_action.setEnabled(active_pane_count > 0)
        self.next_terminal_tab_action.setEnabled(active_pane_count > 1)
        self.previous_terminal_tab_action.setEnabled(active_pane_count > 1)
        pane_count = self.terminal_workspace.pane_count
        maximized = self.terminal_workspace.pane_is_maximized
        self.maximize_pane_action.setEnabled(pane_count > 1)
        self.maximize_pane_action.setChecked(maximized)
        self.maximize_pane_action.setText(
            tr("Restore pane layout") if maximized else tr("Maximize active pane")
        )
        self.maximize_pane_action.setStatusTip(
            tr("Show all terminal panes again")
            if maximized
            else tr("Temporarily expand the active pane without closing other sessions")
        )
        self.split_right_action.setEnabled(self.terminal_workspace.can_split_pane)
        self.pane_layout_action.setEnabled(self.terminal_workspace.accepts_new_terminals)
        self.split_down_action.setEnabled(self.terminal_workspace.can_split_pane)
        self.move_current_to_next_pane_action.setEnabled(pane_count > 1 and active_pane_count > 0)
        for action in (
            self.merge_current_pane_action,
            self.merge_all_panes_action,
            self.focus_next_pane_action,
        ):
            action.setEnabled(pane_count > 1)

    def _apply_theme_colors(self, colors: ThemeColors) -> None:
        """Propagate an application palette through the owned terminal tree."""
        self._refresh_action_icons(colors)
        self._sftp_controller.set_interface_colors(
            colors,
            monochrome=self._settings_controller.displayed_theme
            in (ThemeId.MONOCHROME, ThemeId.MONOCHROME_LIGHT),
        )
        self.terminal_workspace.set_terminal_colors(colors.terminal)
        self.terminal_workspace.set_interface_colors(colors)

    def _apply_terminal_font(self, settings: TerminalFontSettings) -> None:
        """Propagate a persisted terminal font to current and future tabs."""
        self.terminal_workspace.set_terminal_font(settings)

    def _apply_terminal_scrollback_lines(self, lines: int) -> None:
        """Use a persisted history limit for future sessions without truncation."""
        self.terminal_workspace.set_terminal_scrollback_lines(lines)

    def _apply_visual_bell_enabled(self, enabled: bool) -> None:
        """Apply terminal bell feedback to current and future sessions."""
        self.terminal_workspace.set_visual_bell_enabled(enabled)

    def _show_commands_sidebar(self) -> None:
        """Show the existing command library without sending terminal input."""
        self.toggle_sidebar_action.setChecked(True)
        snippets, error = self._command_snippet_controller.sidebar_snapshot()
        self.workspace_sidebar.commands.set_snippets(snippets, error)
        self._update_commands_sidebar_target()
        self._sftp_controller.show_commands()

    def _update_commands_sidebar_target(self) -> None:
        self.workspace_sidebar.commands.set_insertion_available(
            self.terminal_workspace.current_command_target() is not None
        )

    def _set_sidebar_visible(self, visible: bool) -> None:
        """Keep the checkable View action as the sidebar visibility source."""
        self.workspace_sidebar.setVisible(visible)

    def _restore_window_layout(self) -> None:
        """Delegate startup restoration without exposing other window orchestration."""
        self._window_layout_controller.restore()

    def _persist_window_layout_once(self) -> None:
        """Delegate the existing one-attempt close-time layout capture."""
        self._window_layout_controller.persist_once()

    def _open_local_terminal(self, _checked: bool = False) -> None:
        """Ask the terminal workspace to create a local session tab."""
        return terminal_window_lifecycle._open_local_terminal(self, _checked)

    def _open_private_recovery_dialog(self, _checked: bool = False) -> None:
        """Open recovery only on explicit request, with one modeless owner per window."""
        return terminal_window_lifecycle._open_private_recovery_dialog(self, _checked)

    def _release_private_recovery_dialog(self, dialog: QDialog) -> None:
        """A retired dialog cannot clear a newly opened recovery owner."""
        return terminal_window_lifecycle._release_private_recovery_dialog(self, dialog)

    def _show_local_terminal_opened(self, terminal_name: str) -> None:
        """Report creation of a local shell tab."""
        return terminal_window_lifecycle._show_local_terminal_opened(self, terminal_name)

    def _show_terminal_closed(self, terminal_name: str) -> None:
        """Report closure of a terminal tab."""
        return terminal_window_lifecycle._show_terminal_closed(self, terminal_name)

    def _open_connection(self, profile: object) -> _ConnectionOpenState:
        """Dispatch a validated saved profile to its isolated transport path."""
        return connections_window_catalog._open_connection(self, profile)

    def _open_ssh_connection(
        self,
        profile: ConnectionProfile,
        *,
        recovery_attempted: bool = False,
        resolved_login: _SshLoginResolution | None = None,
        origin: _SshConnectOrigin = _SshConnectOrigin.STANDARD,
        reconnect_session_id: UUID | None = None,
        pane_id: UUID | None = None,
        launch_guard: Callable[[], bool] | None = None,
    ) -> _ConnectionOpenState:
        """Open a saved or temporary SSH profile through the shared workspace."""
        return ssh_window_launch._open_ssh_connection(
            self,
            profile,
            recovery_attempted=recovery_attempted,
            resolved_login=resolved_login,
            origin=origin,
            reconnect_session_id=reconnect_session_id,
            pane_id=pane_id,
            launch_guard=launch_guard,
        )

    def _open_resolved_ssh_login(
        self,
        resolved_login: _SshLoginResolution,
        *,
        recovery_attempted: bool,
        pane_id: UUID | None = None,
        launch_guard: Callable[[], bool] | None = None,
        source_session_id: UUID | None = None,
    ) -> bool:
        """Apply one GUI-thread login resolution to the ordinary SSH launch flow."""
        return ssh_window_launch._open_resolved_ssh_login(
            self,
            resolved_login,
            recovery_attempted=recovery_attempted,
            pane_id=pane_id,
            launch_guard=launch_guard,
            source_session_id=source_session_id,
        )

    def _check_ssh_profile_available(self, profile, prompt_session_id=None):
        return ssh_window_launch._check_ssh_profile_available(self, profile, prompt_session_id)

    def _check_system_ssh_available(self, prompt_session_id: UUID | None = None) -> bool:
        """Keep missing native prerequisites out of PATH and credential lookup."""
        return ssh_window_launch._check_system_ssh_available(self, prompt_session_id)

    def _start_resolved_ssh_connection(
        self,
        resolved_login: _SshLoginResolution,
        *,
        recovery_attempted: bool,
        prompt_session_id: UUID | None = None,
        pane_id: UUID | None = None,
        launch_guard: Callable[[], bool] | None = None,
        source_session_id: UUID | None = None,
        host_key_approval: SshHostKeyApproval | None = None,
    ) -> bool:
        """Start OpenSSH directly or promote one validated inline login prompt."""
        return ssh_window_launch._start_resolved_ssh_connection(
            self,
            resolved_login,
            recovery_attempted=recovery_attempted,
            prompt_session_id=prompt_session_id,
            pane_id=pane_id,
            launch_guard=launch_guard,
            source_session_id=source_session_id,
            host_key_approval=host_key_approval,
        )

    def _prepare_embedded_ssh(
        self,
        profile: ConnectionProfile,
        resolved_login: _SshLoginResolution,
        *,
        tunnels_confirmed: bool,
    ) -> tuple["EmbeddedSshBackend", "EmbeddedSshAuthenticationSession"]:
        """Prepare the primary engine without changing routing or falling back."""
        return ssh_window_launch._prepare_embedded_ssh(
            self, profile, resolved_login, tunnels_confirmed=tunnels_confirmed
        )

    def _confirm_current_ssh_tunnels(
        self,
        profile: ConnectionProfile,
        *,
        pane_id: UUID,
        prompt_session_id: UUID | None,
        launch_guard: Callable[[], bool] | None,
        owner_session_id: UUID | None,
    ) -> bool:
        """Keep modal consent bound to one immutable launch and its live owner."""
        return ssh_window_launch._confirm_current_ssh_tunnels(
            self,
            profile,
            pane_id=pane_id,
            prompt_session_id=prompt_session_id,
            launch_guard=launch_guard,
            owner_session_id=owner_session_id,
        )

    def _handle_ssh_login_submitted(
        self,
        session_id: object,
        context: object,
        username: str,
    ) -> None:
        """Promote one terminal-owned ``login as`` response into an SSH process."""
        return ssh_window_launch._handle_ssh_login_submitted(self, session_id, context, username)

    def _create_ssh_authentication(
        self,
        profile: ConnectionProfile,
        resolved_login: _SshLoginResolution,
    ) -> SshAuthenticationSession | None:
        """Prepare the optional Unix AskPass bridge without weakening normal SSH."""
        return ssh_window_launch._create_ssh_authentication(self, profile, resolved_login)

    def _show_ssh_authentication_status(self, message: str) -> None:
        """Render a bounded non-secret status emitted by an auth session."""
        return ssh_window_authentication._show_ssh_authentication_status(self, message)

    def _show_ssh_channel_failure(self, authentication_id: UUID, kind: object) -> None:
        """Route fixed advisory text to the owner, never whichever tab is focused."""
        return ssh_window_authentication._show_ssh_channel_failure(self, authentication_id, kind)

    @Slot(int)
    def _handle_ssh_credential_mutation_pending_count_changed(self, count: int) -> None:
        """Resume an application close after every authorized keychain write settles."""
        return app_window_close._handle_ssh_credential_mutation_pending_count_changed(self, count)

    def _handle_ssh_secret_requested(
        self,
        authentication_id: UUID,
        request: object,
    ) -> None:
        """Forward the existing auth signal to the exact-owner prompt router."""
        return ssh_window_authentication._handle_ssh_secret_requested(
            self, authentication_id, request
        )

    def _handle_ssh_secret_request_finished(
        self,
        authentication_id: UUID,
        request: object,
    ) -> None:
        """Forward completion without resolving the currently selected tab."""
        return ssh_window_authentication._handle_ssh_secret_request_finished(
            self, authentication_id, request
        )

    def _handle_inline_ssh_secret_submitted(
        self,
        terminal_session_id: object,
        request_id: object,
        secret: str,
    ) -> None:
        """Deliver a terminal-owned secret only to its exact live auth session."""
        return ssh_window_authentication._handle_inline_ssh_secret_submitted(
            self, terminal_session_id, request_id, secret
        )

    def _handle_inline_ssh_secret_cancel_requested(
        self,
        terminal_session_id: object,
        request_id: object,
    ) -> None:
        """Cancel only the AskPass request shown in the exact terminal tab."""
        return ssh_window_authentication._handle_inline_ssh_secret_cancel_requested(
            self, terminal_session_id, request_id
        )

    def _cancel_ssh_authentication(self, authentication_id: UUID) -> None:
        """Close the owning terminal after the user cancels a direct AskPass prompt."""
        return ssh_window_authentication._cancel_ssh_authentication(self, authentication_id)

    def _close_ssh_authentication(
        self,
        authentication_id: UUID,
        *,
        collect_diagnostics: bool = True,
    ) -> bytes:
        """Forget one flow and return any private OpenSSH diagnostics it retained."""
        return ssh_window_authentication._close_ssh_authentication(
            self, authentication_id, collect_diagnostics=collect_diagnostics
        )

    def _resolve_ssh_login(
        self,
        profile: ConnectionProfile,
        *,
        recovery_attempted: bool,
        origin: _SshConnectOrigin,
        reconnect_session_id: UUID | None,
        pane_id: UUID,
    ) -> _ConnectionOpenState:
        """Resolve saved-login metadata without blocking the GUI thread."""
        return ssh_window_login_resolution._resolve_ssh_login(
            self,
            profile,
            recovery_attempted=recovery_attempted,
            origin=origin,
            reconnect_session_id=reconnect_session_id,
            pane_id=pane_id,
        )

    def _active_ssh_login_resolution(self) -> _SshLoginResolutionOperation | None:
        """Return the exact active lookup, if it has not already been consumed."""
        return ssh_window_login_resolution._active_ssh_login_resolution(self)

    @staticmethod
    def _same_ssh_connection_profile(
        first: ConnectionProfile | None,
        second: ConnectionProfile,
    ) -> bool:
        """Ignore only catalog decoration when validating a connection intent."""
        return ssh_window_login_resolution._same_ssh_connection_profile(first, second)

    @staticmethod
    def _same_ssh_login_request(
        first: _SshLoginResolutionRequest,
        second: _SshLoginResolutionRequest,
    ) -> bool:
        """Deduplicate only the same immutable connection intent."""
        return ssh_window_login_resolution._same_ssh_login_request(first, second)

    def _abandon_ssh_login_resolution(
        self,
        operation: _SshLoginResolutionOperation,
    ) -> None:
        """Make one superseded operation inert while its native read winds down."""
        return ssh_window_login_resolution._abandon_ssh_login_resolution(self, operation)

    def _set_ssh_login_lookup_pending(
        self,
        request: _SshLoginResolutionRequest | None,
    ) -> None:
        """Expose a compact, non-modal pending state without freezing the UI."""
        return ssh_window_login_resolution._set_ssh_login_lookup_pending(self, request)

    @Slot(object, object)
    def _handle_ssh_login_lookup_succeeded(
        self,
        operation_id: object,
        result: object,
    ) -> None:
        """Apply one non-secret worker result only to its exact current request."""
        return ssh_window_login_resolution._handle_ssh_login_lookup_succeeded(
            self, operation_id, result
        )

    @Slot(object)
    def _handle_ssh_login_lookup_failed(self, operation_id: object) -> None:
        """Fall back to manual authentication after an opaque worker failure."""
        return ssh_window_login_resolution._handle_ssh_login_lookup_failed(self, operation_id)

    def _take_ssh_login_resolution_operation(
        self,
        operation_id: object,
    ) -> _SshLoginResolutionOperation | None:
        """Consume one lookup completion and clear pending UI only when still active."""
        return ssh_window_login_resolution._take_ssh_login_resolution_operation(self, operation_id)

    def _ssh_login_request_is_current(self, request: _SshLoginResolutionRequest) -> bool:
        """Reject late results whose profile, repository, or owning tab changed."""
        return ssh_window_login_resolution._ssh_login_request_is_current(self, request)

    def _ssh_login_request_pane_id(self, request: _SshLoginResolutionRequest) -> UUID | None:
        """Reconnect follows its existing page; an unopened request keeps its pane."""
        return ssh_window_login_resolution._ssh_login_request_pane_id(self, request)

    @staticmethod
    def _valid_ssh_login_lookup_result(
        request: _SshLoginResolutionRequest,
        result: object,
    ) -> bool:
        """Validate every worker-owned metadata field before constructing UI."""
        return ssh_window_login_resolution._valid_ssh_login_lookup_result(request, result)

    def _apply_ssh_login_lookup_result(
        self,
        request: _SshLoginResolutionRequest,
        result: _SshLoginLookupResult,
    ) -> _ConnectionOpenState:
        """Present the existing chooser or manual terminal on the GUI thread."""
        return ssh_window_login_resolution._apply_ssh_login_lookup_result(self, request, result)

    def _open_manual_ssh_login_after_lookup_failure(
        self,
        request: _SshLoginResolutionRequest,
        *,
        notice: str,
        preparation_completed: bool = False,
    ) -> _ConnectionOpenState:
        """Keep Connect usable when a repository lookup cannot complete."""
        return ssh_window_login_resolution._open_manual_ssh_login_after_lookup_failure(
            self, request, notice=notice, preparation_completed=preparation_completed
        )

    def _open_resolved_ssh_lookup(
        self,
        request: _SshLoginResolutionRequest,
        resolution: _SshLoginResolution,
    ) -> _ConnectionOpenState:
        """Launch only while the immutable request still owns a valid UI context."""
        return ssh_window_login_resolution._open_resolved_ssh_lookup(self, request, resolution)

    def _finish_ssh_connect_origin(
        self,
        request: _SshLoginResolutionRequest,
        *,
        succeeded: bool,
    ) -> None:
        """Resolve reconnect only after actual flow acceptance."""
        return ssh_window_login_resolution._finish_ssh_connect_origin(
            self, request, succeeded=succeeded
        )

    def _finish_immediate_ssh_connect_origin(
        self,
        *,
        origin: _SshConnectOrigin,
        reconnect_session_id: UUID | None,
        succeeded: bool,
    ) -> None:
        """Finish one initiating UI transaction after a non-pending launch result."""
        return ssh_window_login_resolution._finish_immediate_ssh_connect_origin(
            self, origin=origin, reconnect_session_id=reconnect_session_id, succeeded=succeeded
        )

    @staticmethod
    def _profile_with_ssh_username(
        profile: ConnectionProfile,
        username: str,
        *,
        is_saved_profile: bool,
    ) -> ConnectionProfile:
        """Apply one selected username and keep unsaved session tab titles precise."""
        return ssh_window_login_resolution._profile_with_ssh_username(
            profile, username, is_saved_profile=is_saved_profile
        )

    @staticmethod
    def _profile_without_ssh_username(
        profile: ConnectionProfile,
        *,
        is_saved_profile: bool,
    ) -> ConnectionProfile:
        """Clear an implicit login while retaining a stable saved-profile title."""
        return ssh_window_login_resolution._profile_without_ssh_username(
            profile, is_saved_profile=is_saved_profile
        )

    def _commit_ssh_preference(
        self,
        request: _SshPreferenceRequest | None,
    ) -> bool:
        """Persist chooser ordering after launch without blocking the GUI thread."""
        return ssh_window_login_resolution._commit_ssh_preference(self, request)

    @Slot(object, object)
    def _handle_ssh_preference_succeeded(
        self,
        operation_id: object,
        result: object,
    ) -> None:
        """Consume one successful non-secret preference write."""
        return ssh_window_login_resolution._handle_ssh_preference_succeeded(
            self, operation_id, result
        )

    @Slot(object)
    def _handle_ssh_preference_failed(self, operation_id: object) -> None:
        """Report a preference-only failure without changing connection success."""
        return ssh_window_login_resolution._handle_ssh_preference_failed(self, operation_id)

    def _take_ssh_preference_operation(
        self,
        operation_id: object,
    ) -> tuple[BackgroundOperation, _SshPreferenceOperationContext] | None:
        """Consume exactly one preference completion identified by UUID."""
        return ssh_window_login_resolution._take_ssh_preference_operation(self, operation_id)

    def _show_ssh_preference_error(self) -> None:
        """Keep a successful terminal open after a retryable preference failure."""
        return ssh_window_login_resolution._show_ssh_preference_error(self)

    def _resume_close_after_ssh_repository_operations(self) -> None:
        """Hold instance ownership until every possible index writer has finished."""
        return app_window_close._resume_close_after_ssh_repository_operations(self)

    @Slot(int)
    def _rdp_pending_count_changed(self, count):
        return app_window_close._rdp_pending_count_changed(self, count)

    def _open_telnet_connection(
        self,
        profile: ConnectionProfile,
        *,
        reconnect_session_id: UUID | None = None,
        pane_id: UUID | None = None,
    ) -> bool:
        """Confirm and open one unencrypted Telnet profile through its own backend."""
        return telnet_window_launch._open_telnet_connection(
            self, profile, reconnect_session_id=reconnect_session_id, pane_id=pane_id
        )

    def _handle_connection_reconnect_requested(
        self,
        session_id: object,
        profile: object,
    ) -> None:
        """Route an ended tab through the ordinary protocol and authentication flow."""
        return terminal_window_lifecycle._handle_connection_reconnect_requested(
            self, session_id, profile
        )

    def _confirm_telnet_connection(self, profile: ConnectionProfile) -> bool:
        """Require fresh consent unless this Telnet endpoint was explicitly remembered."""
        return telnet_window_launch._confirm_telnet_connection(self, profile)

    def _reset_telnet_warnings(self) -> None:
        return telnet_window_launch._reset_telnet_warnings(self)

    def _handle_command_terminal_finished(
        self,
        session_id: object,
        recent_output: bytes,
        context: object,
        exit_code: int,
    ) -> None:
        """Explain SSH failures and offer guarded recovery for host-key conflicts."""
        return terminal_window_lifecycle._handle_command_terminal_finished(
            self, session_id, recent_output, context, exit_code
        )

    def _handle_command_terminal_started(
        self,
        session_id: object,
        context: object,
        process_id: int,
    ) -> None:
        """Bind AskPass to the exact process launched for this terminal tab."""
        return terminal_window_lifecycle._handle_command_terminal_started(
            self, session_id, context, process_id
        )

    def _handle_command_terminal_closing(
        self,
        _session_id: object,
        context: object,
    ) -> None:
        """Revoke AskPass before a manually closed terminal begins shutdown."""
        return terminal_window_lifecycle._handle_command_terminal_closing(
            self, _session_id, context
        )

    def _confirm_ssh_host_key_replacement(
        self,
        profile: ConnectionProfile,
        mismatch: SshHostKeyMismatch,
    ) -> bool:
        """Warn clearly before removing an obsolete local trust record."""
        return ssh_window_authentication._confirm_ssh_host_key_replacement(self, profile, mismatch)

    def _exec_ssh_host_key_dialog(self, dialog: QDialog) -> int:
        """Keep each nested host-key decision owned until its modal loop exits."""
        return ssh_window_authentication._exec_ssh_host_key_dialog(self, dialog)

    def _show_manual_host_key_warning(
        self,
        profile: ConnectionProfile,
        mismatch: SshHostKeyMismatch,
    ) -> None:
        """Explain why ambiguous host identity or trust files require inspection."""
        return ssh_window_authentication._show_manual_host_key_warning(self, profile, mismatch)

    def _show_unverifiable_host_key_warning(
        self,
        profile: ConnectionProfile,
        mismatch: SshHostKeyMismatch,
    ) -> None:
        """Refuse automatic recovery when no fingerprint can be verified."""
        return ssh_window_authentication._show_unverifiable_host_key_warning(
            self, profile, mismatch
        )

    def _handle_known_hosts_removal_finished(
        self,
        context: object,
        succeeded: bool,
        message: str,
    ) -> None:
        """Retry normal OpenSSH verification only after a guarded removal."""
        return ssh_window_authentication._handle_known_hosts_removal_finished(
            self, context, succeeded, message
        )

    def _open_new_connection_dialog(self, _checked: bool = False) -> None:
        """Open a profile form targeting the User sessions root."""
        return connections_window_catalog._open_new_connection_dialog(self, _checked)

    def _open_new_connection_in_folder(self, destination: object) -> None:
        """Open the profile form for one validated catalog destination."""
        return connections_window_catalog._open_new_connection_in_folder(self, destination)

    def _load_connection_catalog(self) -> None:
        """Load the stored tree without allowing a bad file to crash the UI."""
        return connections_window_catalog._load_connection_catalog(self)

    def _open_connection_import_dialog(self, _checked: bool = False) -> None:
        """Select and parse one portable catalog without blocking the interface."""
        return connections_window_transfer._open_connection_import_dialog(self, _checked)

    def _open_connection_export_dialog(self, _checked: bool = False) -> None:
        """Export the entire catalog snapshot captured before the file picker."""
        return connections_window_transfer._open_connection_export_dialog(self, _checked)

    def _open_connection_item_export_dialog(self, item_id: object) -> None:
        """Resolve one exact saved item; a stale target never falls back to all."""
        return connections_window_transfer._open_connection_item_export_dialog(self, item_id)

    def _connection_export_available(self) -> bool:
        """Guard every export entry point, including requests from stale menus."""
        return connections_window_transfer._connection_export_available(self)

    def _export_connection_catalog(
        self,
        catalog: ConnectionCatalog,
        *,
        scope_label: str,
        dialog_title: str,
        include_folder_contents: bool = False,
    ) -> None:
        """Confirm and export exactly the reviewed snapshot, never later additions."""
        return connections_window_transfer._export_connection_catalog(
            self,
            catalog,
            scope_label=scope_label,
            dialog_title=dialog_title,
            include_folder_contents=include_folder_contents,
        )

    def _active_connection_store_path(self) -> Path | None:
        """Return the real working data path when the configured store exposes one."""
        return connections_window_transfer._active_connection_store_path(self)

    def _register_connection_transfer_dialog(self, dialog: QDialog) -> bool:
        """Bind one modal decision to this live window before its nested loop."""
        return connections_window_transfer._register_connection_transfer_dialog(self, dialog)

    def _choose_connection_transfer_path(
        self, *, importing: bool, title: str, default_path: str
    ) -> str:
        """Own the native picker so accepted shutdown can reject it safely."""
        return connections_window_transfer._choose_connection_transfer_path(
            self, importing=importing, title=title, default_path=default_path
        )

    def _confirm_connection_transfer(self, provider: Callable[..., bool], **values: object) -> bool:
        """Register built-in decisions without changing injected provider contracts."""
        return connections_window_transfer._confirm_connection_transfer(self, provider, **values)

    def _start_connection_transfer_operation(
        self,
        *,
        kind: _ConnectionTransferKind,
        path: Path,
        operation: Callable[[], object],
        pending_import_commit: _PendingConnectionImportCommit | None = None,
    ) -> bool:
        """Start the only active transfer and retain it across queued completion."""
        return connections_window_transfer._start_connection_transfer_operation(
            self,
            kind=kind,
            path=path,
            operation=operation,
            pending_import_commit=pending_import_commit,
        )

    @Slot(object, object)
    def _handle_connection_transfer_succeeded(
        self,
        operation_id: object,
        result: object,
    ) -> None:
        """Consume one exact transfer result and ignore duplicates or stale tasks."""
        return connections_window_transfer._handle_connection_transfer_succeeded(
            self, operation_id, result
        )

    @Slot(object)
    def _handle_connection_transfer_failed(self, operation_id: object) -> None:
        """Reduce file, codec, and unexpected worker failures to a safe UI message."""
        return connections_window_transfer._handle_connection_transfer_failed(self, operation_id)

    def _finish_connection_transfer_operation(
        self,
        operation_id: object,
    ) -> _ConnectionTransferOperationContext | None:
        """Release only the exact active operation and restore action availability."""
        return connections_window_transfer._finish_connection_transfer_operation(self, operation_id)

    def _confirm_and_commit_connection_import(
        self,
        source_path: Path,
        decoded: DecodedConnectionTransfer,
    ) -> None:
        """Merge against the latest catalog, preview exact counts, then persist once."""
        return connections_window_transfer._confirm_and_commit_connection_import(
            self, source_path, decoded
        )

    def _show_connection_transfer_failure(self, kind: _ConnectionTransferKind) -> None:
        """Show actionable, non-sensitive failure text for one transfer direction."""
        return connections_window_transfer._show_connection_transfer_failure(self, kind)

    @staticmethod
    def _connection_transfer_completion_message(
        verb: str,
        stats: ConnectionTransferStats,
    ) -> str:
        """Return one compact status summary without exposing endpoint metadata."""
        return connections_window_transfer._connection_transfer_completion_message(verb, stats)

    def _update_connection_transfer_actions(self) -> None:
        """Keep menu availability aligned with catalog safety and active work."""
        return connections_window_transfer._update_connection_transfer_actions(self)

    def _ssh_session_save_available(self) -> bool:
        return connections_window_catalog._ssh_session_save_available(self)

    def _add_connection_profile(
        self,
        profile: ConnectionProfile,
        *,
        parent_folder_id: UUID | None = None,
    ) -> bool:
        """Persist a profile first, then expose it through the interface."""
        return connections_window_catalog._add_connection_profile(
            self, profile, parent_folder_id=parent_folder_id
        )

    def _edit_connection_profile(self, profile: ConnectionProfile) -> None:
        """Edit a profile while preserving its identity and stored position."""
        return connections_window_catalog._edit_connection_profile(self, profile)

    def _set_connection_favorite(self, profile_id: object, is_favorite: bool) -> None:
        """Persist a favorite mark against the current profile, never a stale row."""
        return connections_window_catalog._set_connection_favorite(self, profile_id, is_favorite)

    def _duplicate_connection_profile(self, profile: object) -> None:
        """Persist a credential-free copy directly after one current saved profile."""
        return connections_window_catalog._duplicate_connection_profile(self, profile)

    def _delete_connection_profile(self, profile: ConnectionProfile) -> None:
        """Delete a profile only after explicit confirmation and persistence."""
        return connections_window_catalog._delete_connection_profile(self, profile)

    def _commit_connection_catalog(
        self,
        candidate: ConnectionCatalog,
        *,
        select_item_id: UUID | None = None,
    ) -> bool:
        """Persist a complete candidate before changing memory or the tree."""
        return connections_window_catalog._commit_connection_catalog(
            self, candidate, select_item_id=select_item_id
        )

    def _apply_connection_catalog(
        self,
        candidate: ConnectionCatalog,
        *,
        select_item_id: UUID | None = None,
    ) -> None:
        """Publish a catalog only after its persistence transaction has succeeded."""
        return connections_window_catalog._apply_connection_catalog(
            self, candidate, select_item_id=select_item_id
        )

    def _open_new_connection_folder(self, _checked: bool = False) -> None:
        """Open a folder form targeting the User sessions root."""
        return connections_window_catalog._open_new_connection_folder(self, _checked)

    def _open_new_connection_folder_in_parent(self, parent: object) -> None:
        """Create one empty folder below a validated root or folder."""
        return connections_window_catalog._open_new_connection_folder_in_parent(self, parent)

    def _rename_connection_folder(self, folder_id: object) -> None:
        """Rename one canonical folder without changing its descendants."""
        return connections_window_catalog._rename_connection_folder(self, folder_id)

    def _delete_connection_folder(self, folder_id: object) -> None:
        """Delete an explicitly confirmed empty folder only."""
        return connections_window_catalog._delete_connection_folder(self, folder_id)

    def _move_connection_catalog_item(
        self,
        item_id: object,
        parent_folder_id: object,
        index: object,
    ) -> None:
        """Persist one sidebar move before reprojecting the catalog tree."""
        return connections_window_catalog._move_connection_catalog_item(
            self, item_id, parent_folder_id, index
        )

    def _resolve_connection_folder_id(
        self,
        value: object,
    ) -> tuple[bool, UUID | None]:
        """Resolve one root/folder destination received from the passive sidebar."""
        return connections_window_catalog._resolve_connection_folder_id(self, value)

    def _sibling_connection_folder_names(
        self,
        parent_folder_id: UUID | None,
    ) -> tuple[str, ...]:
        """Return sibling labels for inline duplicate validation in the dialog."""
        return connections_window_catalog._sibling_connection_folder_names(self, parent_folder_id)

    def _confirm_connection_folder_deletion(self, folder: ConnectionFolder) -> bool:
        """Ask before deleting an empty organizational folder."""
        return connections_window_catalog._confirm_connection_folder_deletion(self, folder)

    def _confirm_connection_deletion(self, profile: ConnectionProfile) -> bool:
        """Ask the user before permanently deleting a saved profile."""
        return connections_window_catalog._confirm_connection_deletion(self, profile)

    def _open_settings_dialog(self, _checked: bool = False) -> None:
        """Open one modal editor and keep it open when persistence fails."""
        return app_window_preferences._open_settings_dialog(self, _checked)

    def _open_saved_ssh_logins_dialog(self, _checked: bool = False) -> None:
        """Open the metadata-only manager without unlocking the credential store."""
        return ssh_window_accounts._open_saved_ssh_logins_dialog(self, _checked)

    def _remove_saved_ssh_login_from_dialog(
        self,
        dialog: SavedSshLoginsDialog,
        credential_id: object,
    ) -> None:
        """Start one opaque removal intent without blocking the GUI thread."""
        return ssh_window_accounts._remove_saved_ssh_login_from_dialog(self, dialog, credential_id)

    def _remove_saved_ssh_logins_from_dialog(
        self,
        dialog: SavedSshLoginsDialog,
        credential_ids: object,
    ) -> None:
        """Start one all-or-nothing batch removal outside the GUI thread."""
        return ssh_window_accounts._remove_saved_ssh_logins_from_dialog(
            self, dialog, credential_ids
        )

    def _start_saved_ssh_login_removal(
        self,
        *,
        dialog: SavedSshLoginsDialog,
        repository: SshCredentialRepository,
        credential_ids: frozenset[UUID],
        is_single: bool,
    ) -> None:
        """Own and dispatch one bounded worker while its modal dialog stays pending."""
        return ssh_window_accounts._start_saved_ssh_login_removal(
            self,
            dialog=dialog,
            repository=repository,
            credential_ids=credential_ids,
            is_single=is_single,
        )

    @Slot(object, object)
    def _handle_saved_ssh_login_removal_succeeded(
        self,
        operation_id: object,
        result: object,
    ) -> None:
        """Refresh metadata and release a pending dialog after a worker commit."""
        return ssh_window_accounts._handle_saved_ssh_login_removal_succeeded(
            self, operation_id, result
        )

    @Slot(object)
    def _handle_saved_ssh_login_removal_failed(self, operation_id: object) -> None:
        """Release a pending dialog after a private worker failure."""
        return ssh_window_accounts._handle_saved_ssh_login_removal_failed(self, operation_id)

    def _take_saved_ssh_login_removal(
        self,
        operation_id: object,
    ) -> tuple[BackgroundOperation, _SshCredentialRemovalContext] | None:
        """Consume exactly one queued completion identified by an opaque UUID."""
        return ssh_window_accounts._take_saved_ssh_login_removal(self, operation_id)

    def _update_saved_ssh_logins_action(self) -> None:
        """Expose the manager only when its repository and worker are available."""
        return ssh_window_accounts._update_saved_ssh_logins_action(self)

    def _resume_close_after_saved_ssh_login_removal(
        self,
        dialog: SavedSshLoginsDialog,
    ) -> None:
        """Let an application close continue only after its keychain transaction ends."""
        return app_window_close._resume_close_after_saved_ssh_login_removal(self, dialog)

    def _refresh_saved_ssh_logins_dialog(
        self,
        dialog: SavedSshLoginsDialog,
        repository: SshCredentialRepository,
    ) -> bool:
        """Refresh only non-secret rows after a successful repository commit."""
        return ssh_window_accounts._refresh_saved_ssh_logins_dialog(self, dialog, repository)

    def _show_saved_ssh_login_removal_error(
        self,
        dialog: SavedSshLoginsDialog,
    ) -> None:
        """Report a retryable credential-store failure without exposing internals."""
        return ssh_window_accounts._show_saved_ssh_login_removal_error(self, dialog)

    def _show_saved_ssh_logins_load_error(self) -> None:
        """Distinguish an unreadable metadata index from a genuinely empty list."""
        return ssh_window_accounts._show_saved_ssh_logins_load_error(self)

    def _commit_settings_from_dialog(
        self,
        dialog: SettingsDialog,
        candidate: ApplicationSettings,
    ) -> None:
        """Persist one candidate before applying it and closing the editor."""
        return app_window_preferences._commit_settings_from_dialog(self, dialog, candidate)

    def _show_update_dialog(self, _checked=False):
        return app_window_update_sync._show_update_dialog(self, _checked)

    def _request_update_install(self, job):
        # Use the existing normal-close confirmation for active sessions below.
        # No force-close, kill or bypass of pending catalog/vault/file operations.
        return app_window_update_sync._request_update_install(self, job)

    def _show_sync_dialog(self, _checked=False):
        return app_window_update_sync._show_sync_dialog(self, _checked)

    def _commit_sync_catalog(self, plan, state):
        return app_window_update_sync._commit_sync_catalog(self, plan, state)

    def _disconnect_sync_catalog(self):
        return app_window_update_sync._disconnect_sync_catalog(self)

    def _reload_sync_catalog(self):
        return app_window_update_sync._reload_sync_catalog(self)

    def _show_about_dialog(self, _checked: bool = False) -> None:
        """Show the application identity and required GPL legal notice."""
        return app_window_preferences._show_about_dialog(self, _checked)

    def _ssh_account_choices(self):
        return ssh_window_accounts._ssh_account_choices(self)

    def _manage_ssh_accounts(self, _checked=False):
        return ssh_window_accounts._manage_ssh_accounts(self, _checked)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Keep Qt alive while terminal and known_hosts helpers stop."""
        return app_window_close.closeEvent(
            self, event, _base_close_event=lambda e: super(__class__, self).closeEvent(e)
        )

    def _confirm_application_close(self, active_terminal_count: int) -> bool:
        """Ask once before an application close interrupts live terminal processes."""
        return app_window_close._confirm_application_close(self, active_terminal_count)

    def _finish_close_after_terminal_shutdown(self) -> None:
        """Retry a pending window close after the final backend has stopped."""
        return app_window_close._finish_close_after_terminal_shutdown(self)
