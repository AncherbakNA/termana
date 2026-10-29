"""Main window for the Termana application."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from termana.build_info import support_details, version_label
from termana.i18n import count_noun, tr
from termana.paths import application_config_directory, known_hosts_file_path

if TYPE_CHECKING:
    from termana.credentials.embedded_ssh_session import EmbeddedSshAuthenticationSession
    from termana.terminal.embedded_ssh_backend import EmbeddedSshBackend

from PySide6.QtCore import QRunnable, QSize, Qt, QThreadPool, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QToolButton,
)
from shiboken6 import isValid

from termana import APP_NAME
from termana.command_snippet_controller import CommandSnippetController
from termana.connection_profile_editor import ConnectionProfileEditor
from termana.credentials.background_operation import BackgroundOperation
from termana.credentials.host_key_approval import SshHostKeyApproval
from termana.credentials.repository import (
    SshCredentialRepository,
    SshCredentialRepositoryBusyError,
    SshCredentialRepositoryError,
)
from termana.credentials.ssh_auth import (
    SshAuthenticationSession,
    SshCredentialMutationCoordinator,
    create_ssh_authentication_session,
)
from termana.dialogs import connection_transfer_dialogs as transfer_dialogs_module
from termana.dialogs.action_dialog import ActionDialog
from termana.dialogs.connection_folder_dialog import ConnectionFolderDialog
from termana.dialogs.connection_transfer_dialogs import (
    confirm_connection_export,
    confirm_connection_import,
)
from termana.dialogs.saved_ssh_logins_dialog import SavedSshLoginsDialog
from termana.dialogs.settings_dialog import SettingsDialog
from termana.dialogs.ssh_host_key_dialog import SshHostKeyDialog
from termana.dialogs.ssh_login_dialog import (
    SavedSshLoginOption,
    SshLoginDialog,
    SshLoginSelection,
)
from termana.dialogs.ssh_tunnel_launch_dialog import (
    SshTunnelLaunchDialog,
    confirm_ssh_tunnel_launch,
)
from termana.dialogs.telnet_warning_dialog import confirm_telnet_connection
from termana.dialogs.terminal_grid_dialog import TerminalGridDialog
from termana.initial_window_fit import InitialWindowFit
from termana.models.connection import (
    MAX_CONNECTION_NAME_CHARACTERS,
    ConnectionCatalog,
    ConnectionFolder,
    ConnectionProfile,
    ConnectionProtocol,
    connection_profile_copy_name,
)
from termana.models.connection_export import catalog_for_connection_export
from termana.models.settings import (
    ApplicationSettings,
    SshEngine,
    TerminalFontSettings,
    ThemeId,
)
from termana.models.ssh_credential import (
    SshCredentialRef,
    SshEndpoint,
    validate_ssh_credential_generation,
)
from termana.models.ssh_identity import SshAuthenticationMode
from termana.models.ssh_jump import SshJumpMode
from termana.models.terminal_layout import terminal_grid_layout
from termana.open_terminals_controller import OpenTerminalsController
from termana.qt_lifecycle import schedule_delete_later
from termana.settings_controller import SettingsController
from termana.sftp_controller import SftpController
from termana.ssh_config_controller import SshConfigController
from termana.ssh_group_input_controller import SshGroupInputController
from termana.ssh_secret_prompt_controller import SshSecretPromptController
from termana.ssh_session_details_controller import SshSessionDetailsController
from termana.ssh_session_save_controller import SshSessionSaveController
from termana.storage.command_snippet_store import CommandSnippetStore
from termana.storage.connection_store import ConnectionStore, ConnectionStoreError
from termana.storage.connection_transfer import (
    ConnectionTransferError,
    ConnectionTransferStats,
    DecodedConnectionTransfer,
    decode_connection_transfer,
    encode_connection_catalog,
    merge_connection_transfer,
)
from termana.storage.connection_transfer_io import (
    ConnectionTransferPathError,
    PreparedConnectionExportTarget,
    prepare_connection_export_target,
    read_connection_transfer_bytes,
    write_prepared_connection_transfer_bytes,
)
from termana.storage.settings_store import SettingsStoreError
from termana.storage.telnet_warning_store import TelnetWarningStore, TelnetWarningStoreError
from termana.terminal.backend_factory import TerminalBackendFactory
from termana.terminal.embedded_preflight import (
    embedded_preflight_guidance,
    embedded_preparation_guidance,
)
from termana.terminal.known_hosts import (
    KnownHostsUpdater,
    SshHostKeyMismatch,
    has_ssh_host_key_warning,
    known_hosts_target,
    parse_ssh_host_key_mismatch,
)
from termana.terminal.ssh_command import SSH_PROGRAM, build_ssh_command
from termana.terminal.ssh_connection_diagnostics import (
    ssh_connection_failure_guidance,
    ssh_keepalive_timeout_guidance,
)
from termana.terminal.ssh_target import (
    SshTarget,
    format_ssh_target,
)
from termana.terminal.ssh_tunnel_diagnostics import (
    SshChannelFailure,
    channel_failure_guidance,
    local_forward_failure_guidance,
)
from termana.terminal.tab_shortcuts import (
    TerminalTabAction,
    shortcut_for_terminal_tab_action,
)
from termana.terminal.telnet_backend import TelnetBackendFactory, create_telnet_backend
from termana.terminal.zoom import ZoomAction, shortcuts_for_zoom_action
from termana.terminal_multi_search_controller import TerminalMultiSearchController
from termana.terminal_output_controller import TerminalOutputController
from termana.terminal_zoom_controller import TerminalZoomController
from termana.theme import ThemeColors
from termana.ui_actions import create_window_action
from termana.ui_icons import interface_icon
from termana.ui_toolbar import WORKBENCH_TOOLS, WorkbenchToolBar, workbench_icon_colors
from termana.widgets.connections_sidebar import ConnectionsSidebar
from termana.widgets.readable_status_bar import ReadableStatusBar
from termana.widgets.terminal_workspace import TerminalWorkspace
from termana.widgets.workspace_sidebar import WorkspaceSidebar
from termana.window_fullscreen_controller import WindowFullscreenController
from termana.window_layout_controller import WindowLayoutController
from termana.window_menus import create_window_menus

DEFAULT_WINDOW_SIZE: Final = (1100, 700)
MINIMUM_WINDOW_SIZE: Final = (800, 520)
DEFAULT_SIDEBAR_WIDTH: Final = 286
CONFIRMATION_MESSAGE_TIMEOUT_MS: Final = 5000
PLACEHOLDER_MESSAGE_TIMEOUT_MS: Final = 4000
HOST_KEY_ERROR_MESSAGE_CHARACTERS: Final = 500
SYSTEM_SSH_UNAVAILABLE_GUIDANCE: Final = (
    "System OpenSSH is unavailable. On Windows, install OpenSSH Client in Optional Features; "
    "on Linux or macOS, install or restore the system OpenSSH client. "
    "Restart Termana after installation. Local terminals remain available."
)
WINDOW_STATE_VERSION: Final = 1
CONNECTION_TRANSFER_FILE_FILTER: Final = (
    "Termana connection files (*.termana-connections.json);;JSON files (*.json)"
)
DEFAULT_CONNECTION_EXPORT_NAME: Final = "connections.termana-connections.json"
TOOLBAR_ICON_EXTENT: Final = 28
LOGGER = logging.getLogger(__name__)
SshAuthenticationFactory = Callable[..., SshAuthenticationSession | None]
BackgroundOperationStarter = Callable[[QRunnable], None]
CredentialOperationStarter = BackgroundOperationStarter


def _start_background_operation(task: QRunnable) -> None:
    """Dispatch one bounded application operation on Qt's shared worker pool."""
    QThreadPool.globalInstance().start(task)


def _start_credential_operation(task: QRunnable) -> None:
    """Keep the credential-specific injection seam used by SSH integration tests."""
    _start_background_operation(task)


def _format_network_endpoint(host: str, port: int) -> str:
    """Format an untrusted host and port without leaving IPv6 ambiguous."""
    display_host = host
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        display_host = f"[{host}]"
    return f"{display_host}:{port}"


def _bounded_session_name(name: str) -> str:
    """Fit a validated ephemeral endpoint into the profile display-name bound."""
    if len(name) <= MAX_CONNECTION_NAME_CHARACTERS:
        return name
    marker = "…"
    remaining = MAX_CONNECTION_NAME_CHARACTERS - len(marker)
    prefix_length = remaining // 2
    suffix_length = remaining - prefix_length
    return f"{name[:prefix_length]}{marker}{name[-suffix_length:]}"


def _is_valid_ssh_credential_generation(value: object) -> bool:
    """Validate worker-returned revision metadata without raising into the UI."""
    try:
        validate_ssh_credential_generation(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class _SshSessionContext:
    """Remember which profile owns a command tab and whether it is a retry."""

    profile: ConnectionProfile
    credential_id: UUID | None = None
    credential_generation: int | None = None
    authentication_id: UUID | None = None
    recovery_attempted: bool = False
    system_config: Path | None = None


@dataclass(frozen=True, slots=True)
class _SshPreferenceRequest:
    """Defer one chooser-order preference write until a terminal actually opens."""

    endpoint: SshEndpoint
    credential_id: UUID | None
    profile_id: UUID | None


@dataclass(frozen=True, slots=True)
class _SshLoginPromptContext:
    """Carry a host-only SSH decision while its tab asks for ``login as``."""

    profile: ConnectionProfile
    preference_request: _SshPreferenceRequest | None = None
    recovery_attempted: bool = False
    notice: str | None = None


@dataclass(frozen=True, slots=True)
class _SshHostKeyRecovery:
    """Carry an approved recovery request across an asynchronous file update."""

    failed_session_id: UUID
    profile: ConnectionProfile
    approval: SshHostKeyApproval
    credential_id: UUID | None = None
    credential_generation: int | None = None


@dataclass(frozen=True, slots=True)
class _SshLoginResolution:
    """Carry one exact login decision into the SSH session and future retry."""

    profile: ConnectionProfile
    credential_id: UUID | None = None
    credential_generation: int | None = None
    preference_request: _SshPreferenceRequest | None = None
    notice: str | None = None


class _ConnectionOpenState(Enum):
    """Distinguish an accepted launch from async pending and rejected outcomes."""

    REJECTED = "rejected"
    ACCEPTED = "accepted"
    PENDING = "pending"

    def __bool__(self) -> bool:
        """Preserve existing truth tests while never treating enqueue as success."""
        return self is _ConnectionOpenState.ACCEPTED


class _SshLoginLookupStatus(Enum):
    """Bound worker outcomes to non-secret, user-safe states."""

    READY = "ready"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    PREFLIGHT_REJECTED = "preflight-rejected"


class _SshConnectOrigin(Enum):
    """Identify the UI transaction that owns an asynchronous lookup."""

    STANDARD = "standard"
    RECONNECT = "reconnect"


@dataclass(frozen=True, slots=True)
class _SshLoginLookupResult:
    """Carry only non-secret credential references out of a worker."""

    status: _SshLoginLookupStatus
    endpoint: SshEndpoint
    credentials: tuple[SshCredentialRef, ...] = ()
    explicit_credential_id: UUID | None = None
    explicit_credential_generation: int | None = None
    explicit_username: str | None = None
    guidance: str | None = None


@dataclass(frozen=True, slots=True)
class _SshLoginResolutionRequest:
    """Bind launch settings and catalog freshness to their exact initiating flow."""

    request_id: UUID
    profile: ConnectionProfile
    repository: SshCredentialRepository | None
    endpoint: SshEndpoint
    is_saved_profile: bool
    catalog_profile_snapshot: ConnectionProfile | None
    recovery_attempted: bool
    origin: _SshConnectOrigin
    reconnect_session_id: UUID | None = None
    pane_id: UUID | None = None
    embedded_known_hosts: Path | None = None
    embedded_agent_path: Path | None = None


@dataclass(slots=True)
class _SshLoginResolutionOperation:
    """Own a runnable and expose synchronous completion to injected test starters."""

    task: BackgroundOperation
    request: _SshLoginResolutionRequest
    completed: bool = False
    outcome: _ConnectionOpenState = _ConnectionOpenState.PENDING


@dataclass(slots=True)
class _SshPreferenceOperationContext:
    """Retain one non-secret preference write until its worker completes."""

    request: _SshPreferenceRequest
    repository: SshCredentialRepository
    completed: bool = False
    failed: bool = False


@dataclass(frozen=True, slots=True)
class _SshCredentialRemovalContext:
    """Retain one modal manager until its background transaction reports an outcome."""

    dialog: SavedSshLoginsDialog
    repository: SshCredentialRepository
    credential_ids: frozenset[UUID]
    is_single: bool


class _ConnectionTransferKind(Enum):
    """Identify the one active portable catalog operation."""

    IMPORT = "import"
    IMPORT_COMMIT = "import_commit"
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class _PendingConnectionImportCommit:
    """Keep a confirmed catalog out of Qt signal arguments while it is persisted."""

    catalog: ConnectionCatalog
    stats: ConnectionTransferStats
    select_item_id: UUID


@dataclass(slots=True)
class _ConnectionTransferOperationContext:
    """Retain one background transfer task until its exact completion signal."""

    operation_id: UUID
    kind: _ConnectionTransferKind
    path: Path
    task: BackgroundOperation
    pending_import_commit: _PendingConnectionImportCommit | None = None


def _lookup_ssh_login_options(
    request: _SshLoginResolutionRequest,
) -> _SshLoginLookupResult:
    """Resolve non-secret login choices within one background repository operation."""
    if request.profile.ssh_algorithms.protocol == "1":
        from termana.terminal.legacy_ssh import legacy_helper_path, verify_legacy_frontend
        from termana.terminal.legacy_ssh_backend import validate_legacy_profile

        try:
            validate_legacy_profile(request.profile)
            verify_legacy_frontend(legacy_helper_path())
        except (OSError, RuntimeError, TypeError, ValueError):
            return _SshLoginLookupResult(
                _SshLoginLookupStatus.PREFLIGHT_REJECTED, request.endpoint,
                guidance="SSH1 settings or its reviewed component are unavailable. No fallback was made.",
            )
    elif request.embedded_known_hosts is not None:
        guidance = embedded_preflight_guidance(
            request.profile, request.embedded_known_hosts, request.embedded_agent_path
        )
        if guidance is not None:
            return _SshLoginLookupResult(
                _SshLoginLookupStatus.PREFLIGHT_REJECTED, request.endpoint, guidance=guidance
            )
    if request.profile.ssh_account_id is not None:
        try:
            if request.repository is None:
                raise SshCredentialRepositoryError("Account store is unavailable")
            credential = request.repository.account_reference(
                request.profile.ssh_account_id, request.endpoint
            )
            return _SshLoginLookupResult(
                _SshLoginLookupStatus.READY, request.endpoint,
                explicit_credential_id=credential.credential_id,
                explicit_credential_generation=credential.generation,
                explicit_username=credential.username,
            )
        except SshCredentialRepositoryError:
            return _SshLoginLookupResult(
                _SshLoginLookupStatus.PREFLIGHT_REJECTED, request.endpoint,
                guidance="The linked SSH account is unavailable or busy. Review the profile's account binding.",
            )
    if (
        request.repository is None
        or request.profile.ssh_identity.authentication is SshAuthenticationMode.KEY_ONLY
    ):
        return _SshLoginLookupResult(_SshLoginLookupStatus.READY, request.endpoint)
    try:
        if not request.is_saved_profile and request.profile.username is not None:
            index = request.repository.load_index()
            credential = next(
                (
                    candidate
                    for candidate in index.credentials_for(request.endpoint)
                    if candidate.username == request.profile.username
                ),
                None,
            )
            return _SshLoginLookupResult(
                status=_SshLoginLookupStatus.READY,
                endpoint=request.endpoint,
                explicit_credential_id=(
                    credential.credential_id if credential is not None else None
                ),
                explicit_credential_generation=(
                    credential.generation if credential is not None else None
                ),
            )
        credentials = request.repository.login_references(
            request.endpoint,
            profile_id=(request.profile.profile_id if request.is_saved_profile else None),
        )
    except SshCredentialRepositoryBusyError:
        return _SshLoginLookupResult(
            status=_SshLoginLookupStatus.BUSY,
            endpoint=request.endpoint,
        )
    except SshCredentialRepositoryError:
        return _SshLoginLookupResult(
            status=_SshLoginLookupStatus.UNAVAILABLE,
            endpoint=request.endpoint,
        )
    return _SshLoginLookupResult(
        status=_SshLoginLookupStatus.READY,
        endpoint=request.endpoint,
        credentials=credentials,
    )


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
            self._rdp_identity_repository.pending_count_changed.connect(self._rdp_pending_count_changed)
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
            ssh_engine=lambda: (SshEngine.EMBEDDED if self._embedded_ssh_known_hosts is not None else SshEngine.SYSTEM),
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
            ssh_engine=lambda: (SshEngine.EMBEDDED if self._embedded_ssh_known_hosts is not None else SshEngine.SYSTEM),
        )
        self._connection_profile_editor.message.connect(self.statusBar().showMessage)
        self.terminal_workspace.saved_commands_requested.connect(
            self._command_snippet_controller.open_dialog
        )
        self._terminal_output_controller = TerminalOutputController(self, self.terminal_workspace)
        from termana.session_logging_controller import SessionLoggingController
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
        warning_directory = catalog_path.parent if isinstance(catalog_path, Path) else application_config_directory()
        self._telnet_warning_store = TelnetWarningStore(warning_directory / "telnet-warning-choices.json")
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
        from termana.ssh_gateway_import_controller import SshGatewayImportController
        self._gateway_import_controller = SshGatewayImportController(
            self, self._system_ssh_config_store,
            available=lambda:self._embedded_ssh_known_hosts is None and self._system_ssh_config_store is not None,
        )
        self.import_gateway_settings_action = create_window_action(
            self,tr('Import gateway SSH settings…'),'importGatewaySshSettingsAction',
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
        from termana.rdp.ui import open_rdp_dialog

        self.rdp_connection_action = create_window_action(
            self, tr("RDP connection…"), "rdpConnectionAction",
            lambda _checked=False: open_rdp_dialog(self),
            status_tip=tr("Open an embedded remote Windows desktop"),
        )
        self.new_connection_action = create_window_action(
            self,
            tr("New connection"),
            "newConnectionAction",
            self._open_new_connection_dialog,
            status_tip=tr("Create a saved remote connection"),
            tool_tip=tr("Create a saved remote connection"),
        )
        self.new_folder_action = create_window_action(
            self,
            tr("New folder"),
            "newConnectionFolderAction",
            self._open_new_connection_folder,
            status_tip=tr("Create a folder for saved connections"),
        )
        self.reset_telnet_warnings_action = create_window_action(
            self, tr("Show Telnet warnings again"), "resetTelnetWarningsAction",
            self._reset_telnet_warnings,
            status_tip=tr("Restore the encryption warning for all Telnet destinations"),
        )
        self.import_connections_action = create_window_action(
            self,
            tr("Import connections…"),
            "importConnectionsAction",
            self._open_connection_import_dialog,
            status_tip=tr("Add SSH and Telnet connections from a Termana JSON file"),
        )
        self.export_connections_action = create_window_action(
            self,
            tr("Export all connections…"),
            "exportConnectionsAction",
            self._open_connection_export_dialog,
            status_tip=tr("Save all SSH and Telnet connections without passwords or saved logins"),
        )
        self.local_terminal_action = create_window_action(
            self,
            tr("Local terminal"),
            "localTerminalAction",
            self._open_local_terminal,
            status_tip=tr("Open a terminal on this computer"),
            tool_tip=tr("Open a terminal on this computer"),
            shortcuts=(shortcut_for_terminal_tab_action(TerminalTabAction.NEW_LOCAL),),
            auto_repeat=False,
        )
        self.recover_local_drafts_action = create_window_action(
            self,
            tr("Recover local drafts…"),
            "recoverLocalDraftsAction",
            self._open_private_recovery_dialog,
            status_tip=tr("Review inactive private local copies without reconnecting or uploading"),
        )
        self.close_current_terminal_action = create_window_action(
            self,
            tr("Close current terminal"),
            "closeCurrentTerminalAction",
            self.terminal_workspace.request_close_current_terminal,
            status_tip=tr("Close the current terminal tab"),
            shortcuts=(shortcut_for_terminal_tab_action(TerminalTabAction.CLOSE_CURRENT),),
            auto_repeat=False,
        )
        self.next_terminal_tab_action = create_window_action(
            self,
            tr("Next terminal tab"),
            "nextTerminalTabAction",
            self.terminal_workspace.activate_next_terminal,
            status_tip=tr("Select the next terminal tab"),
            shortcuts=(shortcut_for_terminal_tab_action(TerminalTabAction.NEXT),),
            auto_repeat=False,
        )
        self.previous_terminal_tab_action = create_window_action(
            self,
            tr("Previous terminal tab"),
            "previousTerminalTabAction",
            self.terminal_workspace.activate_previous_terminal,
            status_tip=tr("Select the previous terminal tab"),
            shortcuts=(shortcut_for_terminal_tab_action(TerminalTabAction.PREVIOUS),),
            auto_repeat=False,
        )

        pane_actions = (
            ("split_right_action", tr("Split right"), "splitRightAction", "split_right"),
            ("split_down_action", tr("Split down"), "splitDownAction", "split_down"),
            (
                "move_current_to_next_pane_action",
                tr("Move current tab to next pane"),
                "moveCurrentToNextPaneAction",
                "move_current_to_next_pane",
            ),
            (
                "merge_current_pane_action",
                tr("Merge current pane"),
                "mergeCurrentPaneAction",
                "merge_current_pane",
            ),
            (
                "merge_all_panes_action",
                tr("Merge all panes"),
                "mergeAllPanesAction",
                "merge_all_panes",
            ),
            (
                "focus_next_pane_action",
                tr("Focus next pane"),
                "focusNextPaneAction",
                "focus_next_pane",
            ),
        )
        for attribute, label, object_name, method_name in pane_actions:
            action = create_window_action(
                self,
                label,
                object_name,
                getattr(self.terminal_workspace, method_name),
                status_tip=label,
                auto_repeat=False,
            )
            setattr(self, attribute, action)

        self.merge_all_panes_action.setStatusTip(
            tr("Return to one pane, keeping all tabs and connections open")
        )
        self.pane_layout_menu = QMenu(tr("Split"), self)
        self.pane_layout_action = self.pane_layout_menu.menuAction()
        self.pane_layout_action.setObjectName("paneLayoutAction")
        self.pane_layout_action.setStatusTip(
            tr("Choose a layout, keeping all tabs and connections open")
        )
        for attribute, label, columns, rows in (
            ("pane_columns_action", tr("Two panes side by side (2 columns, 1 row)"), 2, 1),
            ("pane_rows_action", tr("Two panes stacked (1 column, 2 rows)"), 1, 2),
            ("pane_one_action", tr("One pane (Unsplit)"), 1, 1),
        ):
            action = create_window_action(
                self,
                label,
                attribute,
                lambda _checked=False, c=columns, r=rows: self.terminal_workspace.apply_pane_layout(
                    terminal_grid_layout(c, r)
                ),
                auto_repeat=False,
            )
            setattr(self, attribute, action)
            self.pane_layout_menu.addAction(action)
        self.pane_layout_menu.addSeparator()
        self.pane_custom_action = create_window_action(
            self,
            tr("Custom grid…"),
            "paneCustomAction",
            self._choose_terminal_grid,
            auto_repeat=False,
        )
        self.pane_layout_menu.addAction(self.pane_custom_action)

        self.maximize_pane_action = create_window_action(
            self,
            tr("Maximize active pane"),
            "maximizePaneAction",
            self.terminal_workspace.toggle_maximize_pane,
            auto_repeat=False,
            checkable=True,
        )
        self.rename_terminal_tab_action = create_window_action(
            self,
            tr("Rename current tab…"),
            "renameTerminalTabAction",
            self.terminal_workspace.rename_current_terminal,
            status_tip=tr("Give this terminal tab a custom name"),
        )
        self.switch_terminal_action = create_window_action(
            self,
            tr("Switch terminal…"),
            "switchTerminalAction",
            self._open_terminals_controller.open_dialog,
            status_tip=tr("Find and select an open terminal in any pane"),
            auto_repeat=False,
        )
        self.find_open_terminals_action = create_window_action(
            self,
            tr("Find in open terminals…"),
            "findOpenTerminalsAction",
            self._terminal_multi_search_controller.open_dialog,
            status_tip=tr("Search explicit snapshots of selected terminal output"),
            auto_repeat=False,
        )
        self.ssh_session_details_action = create_window_action(
            self,
            tr("SSH connection details…"),
            "sshSessionDetailsAction",
            self._ssh_session_details_controller.open_dialog,
            status_tip=tr("View SSH connection settings, status and diagnostic details"),
        )
        self.remote_files_action = create_window_action(
            self,
            tr("Remote files"),
            "remoteFilesAction",
            self._sftp_controller.open_files,
            status_tip=tr(
                "Browse and transfer files through an authenticated embedded SSH session"
            ),
        )
        self.save_ssh_session_action = create_window_action(
            self,
            tr("Save SSH session as profile…"),
            "saveSshSessionAction",
            self._ssh_session_save_controller.open_dialog,
            status_tip=tr("Save a new profile without destination credentials"),
        )
        self.saved_commands_action = create_window_action(
            self,
            tr("Saved commands…"),
            "savedCommandsAction",
            lambda _checked=False: self._show_commands_sidebar(),
            status_tip=tr("Manage commands and insert without sending Enter"),
        )
        self.find_terminal_action = create_window_action(
            self,
            tr("Find in active terminal"),
            "findTerminalAction",
            self.terminal_workspace.open_current_terminal_search,
            status_tip=tr("Search the output of the active terminal"),
        )
        self.settings_action = create_window_action(
            self,
            tr("Settings"),
            "settingsAction",
            self._open_settings_dialog,
            shortcuts=(QKeySequence(QKeySequence.StandardKey.Preferences),),
            tool_tip=tr("Open application settings"),
        )
        self.saved_ssh_logins_action = create_window_action(
            self,
            tr("Saved SSH logins…"),
            "savedSshLoginsAction",
            self._open_saved_ssh_logins_dialog,
            status_tip=tr("View and remove SSH passwords saved by Termana"),
        )
        self.saved_ssh_logins_action.setEnabled(self._credential_repository is not None)
        self.ssh_accounts_action = create_window_action(
            self, "SSH accounts…", "sshAccountsAction", self._manage_ssh_accounts,
            status_tip="Create reusable SSH accounts and link them to connection profiles",
        )
        self.ssh_accounts_action.setEnabled(self._credential_repository is not None)
        from termana.configuration_backup_ui import ConfigurationBackupUi

        self._configuration_backup_ui = ConfigurationBackupUi(self)
        self.backup_configuration_action = create_window_action(
            self,
            "Back up configuration…",
            "backupConfigurationAction",
            self._configuration_backup_ui.back_up,
        )
        self.restore_configuration_action = create_window_action(
            self,
            "Restore configuration…",
            "restoreConfigurationAction",
            self._configuration_backup_ui.restore,
        )
        self.about_action = create_window_action(
            self,
            tr("About {p0}", p0=f"{APP_NAME}"),
            "aboutAction",
            self._show_about_dialog,
        )
        self.check_updates_action = create_window_action(
            self, "Check for updates…", "checkUpdatesAction", self._show_update_dialog,
            status_tip="Check signed releases without closing sessions",
        )

        self.toggle_sidebar_action = QAction(tr("Sidebar"), self)
        self.toggle_sidebar_action.setObjectName("toggleConnectionsSidebarAction")
        self.toggle_sidebar_action.setCheckable(True)
        self.toggle_sidebar_action.setChecked(True)
        self.toggle_sidebar_action.toggled.connect(self._set_sidebar_visible)

        self.increase_terminal_font_action = create_window_action(
            self,
            tr("Increase terminal font size"),
            "increaseTerminalFontAction",
            self._terminal_zoom_controller.increase,
            status_tip=tr("Increase the font size in every terminal"),
            shortcuts=shortcuts_for_zoom_action(ZoomAction.INCREASE),
            auto_repeat=False,
        )
        self.decrease_terminal_font_action = create_window_action(
            self,
            tr("Decrease terminal font size"),
            "decreaseTerminalFontAction",
            self._terminal_zoom_controller.decrease,
            status_tip=tr("Decrease the font size in every terminal"),
            shortcuts=shortcuts_for_zoom_action(ZoomAction.DECREASE),
            auto_repeat=False,
        )
        self.reset_terminal_font_action = create_window_action(
            self,
            tr("Reset terminal font size"),
            "resetTerminalFontAction",
            self._terminal_zoom_controller.reset,
            status_tip=tr("Restore the default terminal font size"),
            shortcuts=shortcuts_for_zoom_action(ZoomAction.RESET),
            auto_repeat=False,
        )
        self.exit_action = create_window_action(
            self,
            tr("Exit"),
            "exitAction",
            self.close,
            shortcuts=(QKeySequence(QKeySequence.StandardKey.Quit),),
        )
        self._fullscreen_controller = WindowFullscreenController(self)
        self.fullscreen_action = self._fullscreen_controller.action

    def _create_menu_bar(self) -> None:
        """Create the application menu hierarchy."""
        create_window_menus(self)

    def _choose_terminal_grid(self, _checked: bool = False) -> None:
        dialog = TerminalGridDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.terminal_workspace.apply_pane_layout(dialog.selected_layout())
        dialog.deleteLater()

    def _create_toolbar(self) -> None:
        """Present real shared commands as labeled workbench tools, never placeholders."""
        toolbar = WorkbenchToolBar(tr("Main toolbar"))
        toolbar.setObjectName("mainToolBar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(TOOLBAR_ICON_EXTENT, TOOLBAR_ICON_EXTENT))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.main_toolbar = toolbar
        self._apply_toolbar_actions(self._settings_controller.settings.toolbar_actions)
        self._refresh_action_icons(self._settings_controller.colors)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        self.toggle_toolbar_action = toolbar.toggleViewAction()
        self.toggle_toolbar_action.setObjectName("toggleMainToolbarAction")
        self.toggle_toolbar_action.setText(tr("Main toolbar"))

    def _apply_toolbar_actions(self, selected: tuple[str, ...]) -> None:
        """Remove only toolbar presentations, never hide the shared menu QAction."""
        toolbar = self.main_toolbar
        # QToolBar.clear() deletes QMenu.menuAction() under PySide6. Removing
        # presentations individually preserves the Split menu's action owner.
        for action in toolbar.actions():
            toolbar.removeAction(action)
        button_names = {
            "new_connection_action": "toolbarNewConnectionButton",
            "local_terminal_action": "toolbarLocalTerminalButton",
            "settings_action": "toolbarSettingsButton",
        }
        for tool in WORKBENCH_TOOLS:
            if tool.action_attribute not in selected:
                continue
            action = getattr(self, tool.action_attribute)
            action.setIconText(tr(tool.label, context="toolbar"))
            if action.toolTip() == action.text():
                action.setToolTip(action.statusTip() or action.text())
            toolbar.addAction(action)
            button = toolbar.widgetForAction(action)
            if action is self.pane_layout_action and isinstance(button, QToolButton):
                button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            if button is not None:
                button.setObjectName(
                    button_names.get(tool.action_attribute, f"toolbar_{action.objectName()}")
                )
                button.setAccessibleName(action.text())

    def _refresh_action_icons(self, colors: ThemeColors) -> None:
        """Keep toolbar and menu glyphs legible when the live theme changes."""
        tones = workbench_icon_colors(
            colors,
            monochrome=self._settings_controller.displayed_theme
            in (ThemeId.MONOCHROME, ThemeId.MONOCHROME_LIGHT),
        )
        for tool in WORKBENCH_TOOLS:
            action = getattr(self, tool.action_attribute)
            action.setIconVisibleInMenu(False)
            action.setIcon(interface_icon(tool.icon, tones[tool.tone], size=TOOLBAR_ICON_EXTENT))
        for action, icon in (
            (self.pane_columns_action, "split"),
            (self.pane_rows_action, "split_rows"),
            (self.pane_one_action, "single_pane"),
            (self.pane_custom_action, "pane_grid"),
        ):
            action.setIconVisibleInMenu(True)
            action.setIcon(interface_icon(icon, tones["layout"]))

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
        if self._close_waiting_for_terminals:
            return
        self.terminal_workspace.open_local_terminal()

    def _open_private_recovery_dialog(self, _checked: bool = False) -> None:
        """Open recovery only on explicit request, with one modeless owner per window."""
        if self._close_waiting_for_terminals:
            return
        dialog = self._private_recovery_dialog
        if dialog is None or not isValid(dialog):
            from termana.widgets.private_recovery_dialog import PrivateRecoveryDialog

            dialog = PrivateRecoveryDialog(self)
            self._private_recovery_dialog = dialog
            dialog.finished.connect(lambda _result: self._release_private_recovery_dialog(dialog))
            dialog.destroyed.connect(lambda: self._release_private_recovery_dialog(dialog))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _release_private_recovery_dialog(self, dialog: QDialog) -> None:
        """A retired dialog cannot clear a newly opened recovery owner."""
        if self._private_recovery_dialog is dialog:
            self._private_recovery_dialog = None

    def _show_local_terminal_opened(self, terminal_name: str) -> None:
        """Report creation of a local shell tab."""
        self.statusBar().showMessage(
            tr('Local terminal "{p0}" opened.', p0=f"{terminal_name}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _show_terminal_closed(self, terminal_name: str) -> None:
        """Report closure of a terminal tab."""
        self.statusBar().showMessage(
            tr('Terminal "{p0}" closed.', p0=f"{terminal_name}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _open_connection(self, profile: object) -> _ConnectionOpenState:
        """Dispatch a validated saved profile to its isolated transport path."""
        if not isinstance(profile, ConnectionProfile):
            self.statusBar().showMessage(tr("Could not open an invalid connection profile."))
            return _ConnectionOpenState.REJECTED
        if profile.protocol is ConnectionProtocol.SSH:
            return self._open_ssh_connection(profile)
        if profile.protocol is ConnectionProtocol.TELNET:
            return (
                _ConnectionOpenState.ACCEPTED
                if self._open_telnet_connection(profile)
                else _ConnectionOpenState.REJECTED
            )
        if profile.protocol is ConnectionProtocol.RDP:
            from termana.rdp.ui import open_saved_rdp_profile
            if self._connection_catalog.profile_by_id(profile.profile_id) != profile:
                self.statusBar().showMessage(tr("This connection changed. Open it from the current list."))
                return _ConnectionOpenState.REJECTED
            return (_ConnectionOpenState.ACCEPTED if open_saved_rdp_profile(self, profile)
                    else _ConnectionOpenState.REJECTED)
        self.statusBar().showMessage(tr("This connection protocol is not supported yet."))
        return _ConnectionOpenState.REJECTED

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
        if self._close_waiting_for_terminals:
            return _ConnectionOpenState.REJECTED
        if not self._check_ssh_profile_available(profile):
            self._finish_immediate_ssh_connect_origin(
                origin=origin,
                reconnect_session_id=reconnect_session_id,
                succeeded=False,
            )
            return _ConnectionOpenState.REJECTED
        if profile.protocol is not ConnectionProtocol.SSH:
            self.statusBar().showMessage(tr("This connection protocol is not supported yet."))
            return _ConnectionOpenState.REJECTED

        if pane_id is None:
            pane_id = (
                self.terminal_workspace.pane_id_for_session(reconnect_session_id)
                if reconnect_session_id is not None
                else self.terminal_workspace.current_pane_id
            )
        if pane_id is None or not self.terminal_workspace.pane_available(pane_id):
            return _ConnectionOpenState.REJECTED

        if resolved_login is None:
            return self._resolve_ssh_login(
                profile,
                recovery_attempted=recovery_attempted,
                origin=origin,
                reconnect_session_id=reconnect_session_id,
                pane_id=pane_id,
            )
        return (
            _ConnectionOpenState.ACCEPTED
            if self._open_resolved_ssh_login(
                resolved_login,
                recovery_attempted=recovery_attempted,
                pane_id=pane_id,
                launch_guard=launch_guard,
                source_session_id=reconnect_session_id,
            )
            else _ConnectionOpenState.REJECTED
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
        if not self._check_ssh_profile_available(resolved_login.profile):
            return False
        profile = resolved_login.profile
        if pane_id is None:
            pane_id = self.terminal_workspace.current_pane_id
        if not self.terminal_workspace.pane_available(pane_id):
            return False

        if profile.username is None:
            prompt_session_id = self.terminal_workspace.open_ssh_login_prompt_session(
                profile.name,
                _SshLoginPromptContext(
                    profile=profile,
                    preference_request=resolved_login.preference_request,
                    recovery_attempted=recovery_attempted,
                    notice=resolved_login.notice,
                ),
                pane_id=pane_id,
                profile=profile,
            )
            if prompt_session_id is None:
                self.statusBar().showMessage(
                    tr("Could not open the SSH login prompt."),
                    PLACEHOLDER_MESSAGE_TIMEOUT_MS,
                )
                return False
            message = tr(
                'SSH terminal "{p0}" opened. Enter a login in the terminal.', p0=f"{profile.name}"
            )
            if resolved_login.notice:
                message += f" {resolved_login.notice}"
            self.statusBar().showMessage(message, CONFIRMATION_MESSAGE_TIMEOUT_MS)
            return True

        return self._start_resolved_ssh_connection(
            resolved_login,
            recovery_attempted=recovery_attempted,
            pane_id=pane_id,
            launch_guard=launch_guard,
            source_session_id=source_session_id,
        )

    def _check_ssh_profile_available(self, profile, prompt_session_id=None):
        if profile.ssh_algorithms.protocol != "1":
            return self._check_system_ssh_available(prompt_session_id)
        from termana.terminal.legacy_ssh import legacy_helper_path, verify_legacy_frontend
        from termana.terminal.legacy_ssh_backend import validate_legacy_profile

        try:
            validate_legacy_profile(profile)
            verify_legacy_frontend(legacy_helper_path())
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            message = str(error)
            if prompt_session_id is not None:
                self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, message)
            self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)
            return False
        return True

    def _check_system_ssh_available(self, prompt_session_id: UUID | None = None) -> bool:
        """Keep missing native prerequisites out of PATH and credential lookup."""
        if self._embedded_ssh_known_hosts is not None or self._ssh_program is not None:
            return True
        if prompt_session_id is not None:
            self.terminal_workspace.show_ssh_login_prompt_error(
                prompt_session_id, tr(SYSTEM_SSH_UNAVAILABLE_GUIDANCE)
            )
        self.statusBar().showMessage(tr(SYSTEM_SSH_UNAVAILABLE_GUIDANCE))
        return False

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
        profile = resolved_login.profile
        if self._close_waiting_for_terminals:
            return False
        if launch_guard is not None and not launch_guard():
            return False
        if not self._check_ssh_profile_available(profile, prompt_session_id):
            return False
        if profile.username is None:
            raise ValueError("A resolved SSH launch requires a username")
        if host_key_approval is not None and (
            source_session_id is None
            or self._embedded_ssh_known_hosts is not None
            or host_key_approval.endpoint != SshEndpoint(profile.host, profile.port)
            or not self.terminal_workspace.terminal_reconnect_pending(source_session_id)
        ):
            return False
        owner_session_id = prompt_session_id or source_session_id
        if owner_session_id is not None:
            pane_id = self.terminal_workspace.pane_id_for_session(owner_session_id)
            if pane_id is None:
                return False
        elif pane_id is None:
            pane_id = self.terminal_workspace.current_pane_id
        if not self.terminal_workspace.pane_available(pane_id):
            return False

        if profile.ssh_algorithms.protocol == "1":
            from termana.dialogs.legacy_ssh_dialog import confirm_legacy_ssh

            snapshot = self._connection_catalog.profile_by_id(profile.profile_id)

            def legacy_owner_live():
                return (
                    not self._close_waiting_for_terminals
                    and (launch_guard is None or launch_guard())
                    and self.terminal_workspace.pane_available(pane_id)
                    and (owner_session_id is None or self.terminal_workspace.pane_id_for_session(owner_session_id) == pane_id)
                    and self._connection_catalog.profile_by_id(profile.profile_id) == snapshot
                )

            if not confirm_legacy_ssh(profile, self, legacy_owner_live):
                return False

        tunnels_confirmed = False
        if not profile.ssh_tunnels.is_default:
            tunnels_confirmed = self._confirm_current_ssh_tunnels(
                profile,
                pane_id=pane_id,
                prompt_session_id=prompt_session_id,
                launch_guard=launch_guard,
                owner_session_id=owner_session_id,
            )
            if not tunnels_confirmed:
                return False
            if owner_session_id is not None:
                # The nested dialog loop can relocate its source. Replacement
                # belongs to that page's live pane, never its former tab group.
                pane_id = self.terminal_workspace.pane_id_for_session(owner_session_id)
                if pane_id is None or not self.terminal_workspace.pane_available(pane_id):
                    return False

        prepared_backend = None
        system_config = None
        if self._embedded_ssh_known_hosts is not None or profile.ssh_algorithms.protocol == "1":
            try:
                prepared_backend, authentication = self._prepare_embedded_ssh(
                    profile, resolved_login, tunnels_confirmed=tunnels_confirmed
                )
            except (ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
                message = (tr("The SSH1 component could not be prepared safely. No connection was opened.")
                           if profile.ssh_algorithms.protocol == "1" else embedded_preparation_guidance(error))
                if prompt_session_id is not None:
                    self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, message)
                self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)
                return False
            command = None
        else:
            if not self._check_system_ssh_available(prompt_session_id):
                return False
            try:
                system_config = (None if self._system_ssh_config_store is None
                                 else self._system_ssh_config_store.prepare())
            except (OSError, RuntimeError, ValueError):
                message = tr('Termana SSH config could not be verified safely. Review ~/.termana/ssh_config and folder permissions; no SSH connection was opened.')
                if prompt_session_id is not None:
                    self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id,message)
                self.statusBar().showMessage(message,PLACEHOLDER_MESSAGE_TIMEOUT_MS)
                return False
            command = build_ssh_command(
                host=profile.host,
                port=profile.port,
                username=profile.username,
                program=self._ssh_program,
                ssh_cipher=profile.ssh_cipher,
                ssh_algorithms=profile.ssh_algorithms,
                ssh_options=profile.ssh_options,
                ssh_identity=profile.ssh_identity,
                ssh_jump=profile.ssh_jump,
                ssh_tunnels=profile.ssh_tunnels,
                configured_forwards_confirmed=tunnels_confirmed,
                known_hosts=(known_hosts_file_path() if self._system_ssh_config_store is None
                             else self._system_ssh_config_store.trust),
                system_config=system_config,
            )
            authentication = self._create_ssh_authentication(profile, resolved_login)
        manual_command = command
        authentication_id = None
        if host_key_approval is not None and authentication is None:
            return False
        if authentication is not None:
            try:
                if host_key_approval is not None:
                    authentication.approve_host_key_once(host_key_approval)
                authentication_id = authentication.authentication_id
                if not isinstance(authentication_id, UUID):
                    raise TypeError("SSH authentication ID must be a UUID")
                if prepared_backend is None:
                    command = authentication.prepare_command(command)
                self._ssh_authentication_sessions[authentication_id] = authentication
                authentication.status_message.connect(self._show_ssh_authentication_status)
                channel_failures = getattr(authentication, "channel_open_failed", None)
                if not profile.ssh_tunnels.is_default and channel_failures is not None:
                    channel_failures.connect(
                        lambda kind, authentication_id=authentication_id: (
                            self._show_ssh_channel_failure(authentication_id, kind)
                        )
                    )
                authentication.secret_requested.connect(
                    lambda request, authentication_id=authentication_id: (
                        self._handle_ssh_secret_requested(authentication_id, request)
                    )
                )
                authentication.secret_request_finished.connect(
                    lambda request, authentication_id=authentication_id: (
                        self._handle_ssh_secret_request_finished(authentication_id, request)
                    )
                )
                authentication.cancel_requested.connect(
                    lambda authentication_id=authentication_id: self._cancel_ssh_authentication(
                        authentication_id
                    )
                )
            except (AttributeError, RuntimeError, TypeError, ValueError):
                LOGGER.warning("Could not prepare secure SSH password handling")
                command = manual_command
                if (
                    isinstance(authentication_id, UUID)
                    and authentication_id in self._ssh_authentication_sessions
                ):
                    self._close_ssh_authentication(
                        authentication_id,
                        collect_diagnostics=False,
                    )
                else:
                    try:
                        authentication.close()
                    finally:
                        authentication.setParent(None)
                authentication = None
                authentication_id = None
                if prepared_backend is not None:
                    prepared_backend.close()
                    prepared_backend.deleteLater()
                    return False
                if host_key_approval is not None:
                    return False

        if self._close_waiting_for_terminals or (launch_guard is not None and not launch_guard()):
            if authentication_id is not None:
                self._close_ssh_authentication(authentication_id, collect_diagnostics=False)
            return False

        session_context = _SshSessionContext(
            profile=profile,
            credential_id=resolved_login.credential_id,
            credential_generation=resolved_login.credential_generation,
            authentication_id=authentication_id,
            recovery_attempted=recovery_attempted,
            system_config=system_config,
        )
        try:
            if prepared_backend is not None and prompt_session_id is None:
                terminal_session_id = self.terminal_workspace.open_backend_terminal_session(
                    profile.name,
                    prepared_backend,
                    requires_direct_input=True,
                    session_context=session_context,
                    reconnect_profile=profile,
                    pane_id=pane_id,
                )
            elif prepared_backend is not None:
                terminal_session_id = self.terminal_workspace.promote_ssh_login_prompt_backend(
                    prompt_session_id,
                    prepared_backend,
                    session_context,
                    terminal_name=profile.name,
                    requires_direct_input=True,
                    reconnect_profile=profile,
                )
            elif host_key_approval is not None:
                terminal_session_id = (
                    self.terminal_workspace.replace_ended_command_terminal_session(
                        source_session_id, command, session_context
                    )
                )
            elif prompt_session_id is None:
                terminal_session_id = self.terminal_workspace.open_command_terminal_session(
                    profile.name,
                    command,
                    requires_direct_input=True,
                    session_context=session_context,
                    reconnect_profile=profile,
                    pane_id=pane_id,
                )
            else:
                terminal_session_id = self.terminal_workspace.promote_ssh_login_prompt_session(
                    prompt_session_id,
                    command,
                    session_context,
                    terminal_name=profile.name,
                    requires_direct_input=True,
                    reconnect_profile=profile,
                )
        except (OSError, RuntimeError, TypeError, ValueError):
            # Adapter/backend exceptions can contain command or credential data.
            LOGGER.warning("Could not start the SSH terminal")
            if authentication_id is not None:
                self._close_ssh_authentication(
                    authentication_id,
                    collect_diagnostics=False,
                )
            message = tr("Could not start SSH. Please try again.")
            if prompt_session_id is not None:
                self.terminal_workspace.show_ssh_login_prompt_error(
                    prompt_session_id,
                    message,
                )
            self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)
            return False
        if terminal_session_id is None:
            if authentication_id is not None:
                self._close_ssh_authentication(authentication_id, collect_diagnostics=False)
            message = tr("Interactive SSH is not available on this platform yet.")
            if prompt_session_id is not None:
                message = tr("Could not start SSH. Check the login and try again.")
                self.terminal_workspace.show_ssh_login_prompt_error(
                    prompt_session_id,
                    message,
                )
            self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)
            return False
        if (
            authentication_id is not None
            and authentication_id in self._ssh_authentication_sessions
            and self.terminal_workspace.contains_terminal_session(terminal_session_id)
        ):
            self._ssh_authentication_terminal_ids[authentication_id] = terminal_session_id
            if authentication_id in self._pending_ssh_authentication_cancellations:
                self._pending_ssh_authentication_cancellations.discard(authentication_id)
                self.terminal_workspace.close_terminal_session(terminal_session_id)
                return False

        preference_error = self._commit_ssh_preference(resolved_login.preference_request)
        message = tr('SSH terminal "{p0}" opened.', p0=f"{profile.name}")
        if preference_error:
            message += tr(" The login choice could not be remembered.")
        elif resolved_login.notice:
            message += f" {resolved_login.notice}"
        self.statusBar().showMessage(message, CONFIRMATION_MESSAGE_TIMEOUT_MS)
        return True

    def _prepare_embedded_ssh(
        self,
        profile: ConnectionProfile,
        resolved_login: _SshLoginResolution,
        *,
        tunnels_confirmed: bool,
    ) -> tuple["EmbeddedSshBackend", "EmbeddedSshAuthenticationSession"]:
        """Prepare the primary engine without changing routing or falling back."""
        from termana.credentials.embedded_ssh_auth import require_embedded_auth_contract
        from termana.credentials.embedded_ssh_session import EmbeddedSshAuthenticationSession
        from termana.terminal.embedded_ssh import EmbeddedSshPlan
        from termana.terminal.embedded_ssh_backend import EmbeddedSshBackend

        authentication_id = uuid4()
        if profile.ssh_algorithms.protocol == "1":
            from termana.terminal.legacy_ssh import legacy_helper_path
            from termana.terminal.legacy_ssh_backend import LegacyConnectionPlan, LegacySshBackend

            trust = (self._embedded_ssh_known_hosts or known_hosts_file_path()).with_name("ssh1_known_hosts.json")
            plan = LegacyConnectionPlan(profile, trust, legacy_helper_path())
            backend = LegacySshBackend(plan, authentication_id, insecure_protocol_approved=True)
        else:
            require_embedded_auth_contract()
            plan = EmbeddedSshPlan(
                profile,
                self._embedded_ssh_known_hosts,
                tunnels_confirmed,
                agent_path=self._embedded_ssh_agent_path,
            )
            plan.connection_options()
            backend = EmbeddedSshBackend(plan, authentication_id, interactive_host_trust=True)
        catalog_snapshot = self._connection_catalog.profile_by_id(profile.profile_id)

        def owner_live():
            session_id = self._ssh_authentication_terminal_ids.get(authentication_id)
            return (
                not self._close_waiting_for_terminals
                and authentication_id in self._ssh_authentication_sessions
                and session_id is not None
                and self.terminal_workspace.contains_terminal_session(session_id)
                and (
                    catalog_snapshot is None
                    or self._same_ssh_connection_profile(
                        self._connection_catalog.profile_by_id(profile.profile_id), catalog_snapshot
                    )
                )
            )

        authentication = EmbeddedSshAuthenticationSession(
            backend,
            repository=self._credential_repository,
            credential_id=resolved_login.credential_id,
            credential_generation=resolved_login.credential_generation,
            credential_mutation_coordinator=self._ssh_credential_mutation_coordinator,
            owner_live=owner_live,
            operation_starter=self._credential_operation_starter,
            parent=self,
        )
        return backend, authentication

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
        repository = self._credential_repository
        store = self._connection_store
        ssh_program = self._ssh_program
        catalog_snapshot = self._connection_catalog.profile_by_id(profile.profile_id)
        submission = (
            self.terminal_workspace.ssh_login_prompt_submission(prompt_session_id)
            if prompt_session_id is not None
            else None
        )

        def current() -> bool:
            current_pane_id = (
                self.terminal_workspace.pane_id_for_session(owner_session_id)
                if owner_session_id is not None
                else pane_id
            )
            if (
                self._close_waiting_for_terminals
                or current_pane_id is None
                or not self.terminal_workspace.pane_available(current_pane_id)
                or self._credential_repository is not repository
                or self._connection_store is not store
                or self._ssh_program != ssh_program
                or (launch_guard is not None and not launch_guard())
            ):
                return False
            catalog_now = self._connection_catalog.profile_by_id(profile.profile_id)
            if catalog_snapshot is None:
                if catalog_now is not None:
                    return False
            elif not self._same_ssh_connection_profile(catalog_now, catalog_snapshot):
                return False
            if prompt_session_id is not None:
                now = self.terminal_workspace.ssh_login_prompt_submission(prompt_session_id)
                return (
                    submission is not None
                    and now is not None
                    and now[0] is submission[0]
                    and now[1] == submission[1] == profile.username
                )
            return True

        message = tr("SSH tunnel connection cancelled.")
        if self._ssh_tunnel_confirmation_active or not current():
            if prompt_session_id is not None:
                self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, message)
            return False
        self._ssh_tunnel_confirmation_active = True

        def retain_dialog(dialog: SshTunnelLaunchDialog) -> None:
            self._ssh_tunnel_launch_dialog = dialog

        try:
            confirmed = confirm_ssh_tunnel_launch(
                profile=profile,
                parent=self,
                dialog_created=retain_dialog,
                inherited_config=not (
                    self._embedded_ssh_known_hosts is None
                    and self._system_ssh_config_store is not None
                ),
            )
            if confirmed is True and current():
                return True
            if confirmed is True:
                message = tr(
                    "The SSH connection changed while reviewing tunnels. Please connect again."
                )
        except (OSError, RuntimeError, TypeError, ValueError):
            message = tr("Could not confirm SSH tunnels. Please connect again.")
        finally:
            self._ssh_tunnel_launch_dialog = None
            self._ssh_tunnel_confirmation_active = False
        if prompt_session_id is not None:
            self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, message)
        if not self._close_waiting_for_terminals:
            self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)
        return False

    def _handle_ssh_login_submitted(
        self,
        session_id: object,
        context: object,
        username: str,
    ) -> None:
        """Promote one terminal-owned ``login as`` response into an SSH process."""
        if (
            self._close_waiting_for_terminals
            or not isinstance(session_id, UUID)
            or not isinstance(context, _SshLoginPromptContext)
        ):
            return
        is_saved_profile = (
            self._connection_catalog.profile_by_id(context.profile.profile_id) is not None
        )
        try:
            profile = self._profile_with_ssh_username(
                context.profile,
                username,
                is_saved_profile=is_saved_profile,
            )
        except ValueError:
            self.terminal_workspace.show_ssh_login_prompt_error(
                session_id,
                tr("Enter a valid SSH username."),
            )
            return
        self._start_resolved_ssh_connection(
            _SshLoginResolution(
                profile=profile,
                preference_request=context.preference_request,
                notice=context.notice,
            ),
            recovery_attempted=context.recovery_attempted,
            prompt_session_id=session_id,
        )

    def _create_ssh_authentication(
        self,
        profile: ConnectionProfile,
        resolved_login: _SshLoginResolution,
    ) -> SshAuthenticationSession | None:
        """Prepare the optional Unix AskPass bridge without weakening normal SSH."""
        repository = self._credential_repository
        if repository is None:
            return None
        try:
            endpoint = SshEndpoint(profile.host, profile.port)
            saved_profile = self._connection_catalog.profile_by_id(profile.profile_id)
            profile_id = profile.profile_id if saved_profile is not None else None
            return self._ssh_authentication_factory(
                repository=repository,
                endpoint=endpoint,
                username=profile.username,
                credential_id=resolved_login.credential_id,
                credential_generation=resolved_login.credential_generation,
                profile_id=profile_id,
                ssh_program=self._ssh_program,
                parent=self,
                secret_prompt=None,
                credential_operation_starter=self._credential_operation_starter,
                credential_mutation_coordinator=(self._ssh_credential_mutation_coordinator),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            LOGGER.warning("Secure SSH password handling is unavailable")
            return None

    def _show_ssh_authentication_status(self, message: str) -> None:
        """Render a bounded non-secret status emitted by an auth session."""
        if self._close_waiting_for_terminals or not isinstance(message, str):
            return
        self.statusBar().showMessage(message[:HOST_KEY_ERROR_MESSAGE_CHARACTERS])

    def _show_ssh_channel_failure(self, authentication_id: UUID, kind: object) -> None:
        """Route fixed advisory text to the owner, never whichever tab is focused."""
        if (
            self._close_waiting_for_terminals
            or type(kind) is not SshChannelFailure
            or authentication_id not in self._ssh_authentication_sessions
        ):
            return
        session_id = self._ssh_authentication_terminal_ids.get(authentication_id)
        if session_id is not None:
            self.terminal_workspace.show_connection_guidance(
                session_id, channel_failure_guidance(kind)
            )

    @Slot(int)
    def _handle_ssh_credential_mutation_pending_count_changed(self, count: int) -> None:
        """Resume an application close after every authorized keychain write settles."""
        if count == 0 and self._close_waiting_for_terminals and self._terminal_shutdown_complete:
            QTimer.singleShot(0, self.close)

    def _handle_ssh_secret_requested(
        self,
        authentication_id: UUID,
        request: object,
    ) -> None:
        """Forward the existing auth signal to the exact-owner prompt router."""
        self._ssh_secret_prompts.request(authentication_id, request)

    def _handle_ssh_secret_request_finished(
        self,
        authentication_id: UUID,
        request: object,
    ) -> None:
        """Forward completion without resolving the currently selected tab."""
        self._ssh_secret_prompts.finish(authentication_id, request)

    def _handle_inline_ssh_secret_submitted(
        self,
        terminal_session_id: object,
        request_id: object,
        secret: str,
    ) -> None:
        """Deliver a terminal-owned secret only to its exact live auth session."""
        try:
            self._ssh_secret_prompts.submit(terminal_session_id, request_id, secret)
        finally:
            secret = ""

    def _handle_inline_ssh_secret_cancel_requested(
        self,
        terminal_session_id: object,
        request_id: object,
    ) -> None:
        """Cancel only the AskPass request shown in the exact terminal tab."""
        self._ssh_secret_prompts.cancel(terminal_session_id, request_id)

    def _cancel_ssh_authentication(self, authentication_id: UUID) -> None:
        """Close the owning terminal after the user cancels a direct AskPass prompt."""
        if authentication_id not in self._ssh_authentication_sessions:
            self._pending_ssh_authentication_cancellations.discard(authentication_id)
            return
        terminal_session_id = self._ssh_authentication_terminal_ids.get(authentication_id)
        if terminal_session_id is None:
            self._pending_ssh_authentication_cancellations.add(authentication_id)
            return
        self.terminal_workspace.close_terminal_session(terminal_session_id)

    def _close_ssh_authentication(
        self,
        authentication_id: UUID,
        *,
        collect_diagnostics: bool = True,
    ) -> bytes:
        """Forget one flow and return any private OpenSSH diagnostics it retained."""
        authentication = self._ssh_authentication_sessions.pop(authentication_id, None)
        self._ssh_authentication_terminal_ids.pop(authentication_id, None)
        self._pending_ssh_authentication_cancellations.discard(authentication_id)
        if authentication is None:
            return b""
        try:
            if collect_diagnostics:
                return authentication.finish()
            authentication.close()
            return b""
        finally:
            # The factory gives sessions a MainWindow parent. Detaching after shutdown
            # releases their closed QLocalSocket child graph during long app runtimes.
            authentication.setParent(None)
            self._ssh_secret_prompts.clear_authentication(authentication_id)

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
        saved_profile = self._connection_catalog.profile_by_id(profile.profile_id)
        is_saved_profile = saved_profile is not None
        catalog_profile_snapshot = profile if is_saved_profile else None
        if saved_profile is not None and origin is _SshConnectOrigin.RECONNECT:
            # An ended session retains the chosen login, not the catalog username.
            # Retain the tab's connection settings, but independently capture the
            # current catalog to reject only edits made during this new lookup.
            catalog_profile_snapshot = saved_profile
            profile = replace(profile, username=saved_profile.username)
        manual_profile = self._profile_without_ssh_username(
            profile,
            is_saved_profile=is_saved_profile,
        )
        connection_profile = manual_profile if is_saved_profile else profile
        repository = self._credential_repository
        launch_guard = (
            (lambda: self.terminal_workspace.terminal_reconnect_pending(reconnect_session_id))
            if reconnect_session_id is not None
            else None
        )
        if self._embedded_ssh_known_hosts is None and (
            repository is None
            or profile.ssh_identity.authentication is SshAuthenticationMode.KEY_ONLY
        ):
            outcome = (
                _ConnectionOpenState.ACCEPTED
                if self._open_resolved_ssh_login(
                    _SshLoginResolution(profile=connection_profile),
                    recovery_attempted=recovery_attempted,
                    pane_id=pane_id,
                    launch_guard=launch_guard,
                    source_session_id=reconnect_session_id,
                )
                else _ConnectionOpenState.REJECTED
            )
            self._finish_immediate_ssh_connect_origin(
                origin=origin,
                reconnect_session_id=reconnect_session_id,
                succeeded=bool(outcome),
            )
            return outcome

        try:
            endpoint = SshEndpoint(profile.host, profile.port)
        except ValueError:
            LOGGER.warning("Skipping saved SSH login lookup for an unsupported endpoint")
            outcome = (
                _ConnectionOpenState.ACCEPTED
                if self._open_resolved_ssh_login(
                    _SshLoginResolution(profile=connection_profile),
                    recovery_attempted=recovery_attempted,
                    pane_id=pane_id,
                    launch_guard=launch_guard,
                    source_session_id=reconnect_session_id,
                )
                else _ConnectionOpenState.REJECTED
            )
            self._finish_immediate_ssh_connect_origin(
                origin=origin,
                reconnect_session_id=reconnect_session_id,
                succeeded=bool(outcome),
            )
            return outcome

        request = _SshLoginResolutionRequest(
            request_id=uuid4(),
            profile=profile,
            repository=repository,
            endpoint=endpoint,
            is_saved_profile=is_saved_profile,
            catalog_profile_snapshot=catalog_profile_snapshot,
            recovery_attempted=recovery_attempted,
            origin=origin,
            reconnect_session_id=reconnect_session_id,
            pane_id=pane_id,
            embedded_known_hosts=self._embedded_ssh_known_hosts,
            embedded_agent_path=self._embedded_ssh_agent_path,
        )
        active = self._active_ssh_login_resolution()
        if active is not None and self._same_ssh_login_request(active.request, request):
            return _ConnectionOpenState.PENDING
        if active is not None:
            self._abandon_ssh_login_resolution(active)

        task = BackgroundOperation(
            request.request_id,
            lambda: _lookup_ssh_login_options(request),
        )
        operation = _SshLoginResolutionOperation(task=task, request=request)
        self._ssh_login_resolution_operations[request.request_id] = operation
        self._active_ssh_login_resolution_id = request.request_id
        task.signals.succeeded.connect(self._handle_ssh_login_lookup_succeeded)
        task.signals.failed.connect(self._handle_ssh_login_lookup_failed)
        self._set_ssh_login_lookup_pending(request)
        try:
            self._credential_operation_starter(task)
        except Exception:
            if request.request_id not in self._ssh_login_resolution_operations:
                return operation.outcome
            self._ssh_login_resolution_operations.pop(request.request_id, None)
            if self._active_ssh_login_resolution_id == request.request_id:
                self._active_ssh_login_resolution_id = None
                self._set_ssh_login_lookup_pending(None)
            if operation.completed:
                self._resume_close_after_ssh_repository_operations()
                return operation.outcome
            LOGGER.warning("The saved SSH login background operation could not start")
            outcome = self._open_manual_ssh_login_after_lookup_failure(
                request,
                notice=tr("Saved logins are unavailable; password entry remains manual."),
            )
            operation.completed = True
            operation.outcome = outcome
            self._finish_ssh_connect_origin(request, succeeded=bool(outcome))
            self._resume_close_after_ssh_repository_operations()
            return outcome
        return operation.outcome if operation.completed else _ConnectionOpenState.PENDING

    def _active_ssh_login_resolution(self) -> _SshLoginResolutionOperation | None:
        """Return the exact active lookup, if it has not already been consumed."""
        operation_id = self._active_ssh_login_resolution_id
        if operation_id is None:
            return None
        return self._ssh_login_resolution_operations.get(operation_id)

    @staticmethod
    def _same_ssh_connection_profile(
        first: ConnectionProfile | None,
        second: ConnectionProfile,
    ) -> bool:
        """Ignore only catalog decoration when validating a connection intent."""
        return first is not None and replace(first, is_favorite=second.is_favorite) == second

    @staticmethod
    def _same_ssh_login_request(
        first: _SshLoginResolutionRequest,
        second: _SshLoginResolutionRequest,
    ) -> bool:
        """Deduplicate only the same immutable connection intent."""
        return (
            MainWindow._same_ssh_connection_profile(first.profile, second.profile)
            and first.is_saved_profile == second.is_saved_profile
            and (
                not first.is_saved_profile
                or (
                    first.catalog_profile_snapshot is not None
                    and second.catalog_profile_snapshot is not None
                    and MainWindow._same_ssh_connection_profile(
                        first.catalog_profile_snapshot, second.catalog_profile_snapshot
                    )
                )
            )
            and first.repository is second.repository
            and first.recovery_attempted == second.recovery_attempted
            and first.origin is second.origin
            and first.reconnect_session_id == second.reconnect_session_id
            and first.pane_id == second.pane_id
            and first.embedded_known_hosts == second.embedded_known_hosts
            and first.embedded_agent_path == second.embedded_agent_path
        )

    def _abandon_ssh_login_resolution(
        self,
        operation: _SshLoginResolutionOperation,
    ) -> None:
        """Make one superseded operation inert while its native read winds down."""
        if operation.completed:
            return
        operation.completed = True
        operation.outcome = _ConnectionOpenState.REJECTED
        if self._active_ssh_login_resolution_id == operation.request.request_id:
            self._active_ssh_login_resolution_id = None
        self._finish_ssh_connect_origin(operation.request, succeeded=False)

    def _set_ssh_login_lookup_pending(
        self,
        request: _SshLoginResolutionRequest | None,
    ) -> None:
        """Expose a compact, non-modal pending state without freezing the UI."""
        pending = request is not None
        self._ssh_login_progress.setVisible(pending)
        if pending:
            self.statusBar().showMessage(tr("Checking saved SSH logins…"))

    @Slot(object, object)
    def _handle_ssh_login_lookup_succeeded(
        self,
        operation_id: object,
        result: object,
    ) -> None:
        """Apply one non-secret worker result only to its exact current request."""
        operation = self._take_ssh_login_resolution_operation(operation_id)
        if operation is None or operation.completed:
            return
        request = operation.request
        if not self._ssh_login_request_is_current(request):
            operation.completed = True
            operation.outcome = _ConnectionOpenState.REJECTED
            if not self._close_waiting_for_terminals:
                self.statusBar().showMessage(
                    tr("That SSH connection request is no longer current."),
                    PLACEHOLDER_MESSAGE_TIMEOUT_MS,
                )
            self._finish_ssh_connect_origin(request, succeeded=False)
            return
        if not self._valid_ssh_login_lookup_result(request, result):
            LOGGER.warning("A saved SSH login lookup returned an invalid result")
            outcome = self._open_manual_ssh_login_after_lookup_failure(
                request,
                notice=tr("Saved logins are unavailable; password entry remains manual."),
            )
        else:
            outcome = self._apply_ssh_login_lookup_result(request, result)
        operation.completed = True
        operation.outcome = outcome
        self._finish_ssh_connect_origin(request, succeeded=bool(outcome))

    @Slot(object)
    def _handle_ssh_login_lookup_failed(self, operation_id: object) -> None:
        """Fall back to manual authentication after an opaque worker failure."""
        operation = self._take_ssh_login_resolution_operation(operation_id)
        if operation is None or operation.completed:
            return
        request = operation.request
        if not self._ssh_login_request_is_current(request):
            operation.completed = True
            operation.outcome = _ConnectionOpenState.REJECTED
            if not self._close_waiting_for_terminals:
                self.statusBar().showMessage(
                    tr("That SSH connection request is no longer current."),
                    PLACEHOLDER_MESSAGE_TIMEOUT_MS,
                )
            self._finish_ssh_connect_origin(request, succeeded=False)
            return
        LOGGER.warning("A saved SSH login background lookup failed")
        outcome = self._open_manual_ssh_login_after_lookup_failure(
            request,
            notice=tr("Saved logins are unavailable; password entry remains manual."),
        )
        operation.completed = True
        operation.outcome = outcome
        self._finish_ssh_connect_origin(request, succeeded=bool(outcome))

    def _take_ssh_login_resolution_operation(
        self,
        operation_id: object,
    ) -> _SshLoginResolutionOperation | None:
        """Consume one lookup completion and clear pending UI only when still active."""
        if not isinstance(operation_id, UUID):
            LOGGER.warning("A saved SSH login lookup returned an invalid identity")
            return None
        operation = self._ssh_login_resolution_operations.pop(operation_id, None)
        if self._active_ssh_login_resolution_id == operation_id:
            self._active_ssh_login_resolution_id = None
            self._set_ssh_login_lookup_pending(None)
        if operation is not None:
            self._resume_close_after_ssh_repository_operations()
        return operation

    def _ssh_login_request_is_current(self, request: _SshLoginResolutionRequest) -> bool:
        """Reject late results whose profile, repository, or owning tab changed."""
        pane_id = self._ssh_login_request_pane_id(request)
        if (
            self._close_waiting_for_terminals
            or self._credential_repository is not request.repository
            or self._embedded_ssh_known_hosts != request.embedded_known_hosts
            or self._embedded_ssh_agent_path != request.embedded_agent_path
            or pane_id is None
            or not self.terminal_workspace.pane_available(pane_id)
        ):
            return False
        if request.is_saved_profile:
            catalog_snapshot = request.catalog_profile_snapshot
            if (
                catalog_snapshot is None
                or catalog_snapshot.profile_id != request.profile.profile_id
                or not self._same_ssh_connection_profile(
                    self._connection_catalog.profile_by_id(request.profile.profile_id),
                    catalog_snapshot,
                )
            ):
                return False
        reconnect_session_id = request.reconnect_session_id
        return reconnect_session_id is None or self.terminal_workspace.terminal_reconnect_pending(
            reconnect_session_id
        )

    def _ssh_login_request_pane_id(self, request: _SshLoginResolutionRequest) -> UUID | None:
        """Reconnect follows its existing page; an unopened request keeps its pane."""
        if request.reconnect_session_id is not None:
            return self.terminal_workspace.pane_id_for_session(request.reconnect_session_id)
        return request.pane_id

    @staticmethod
    def _valid_ssh_login_lookup_result(
        request: _SshLoginResolutionRequest,
        result: object,
    ) -> bool:
        """Validate every worker-owned metadata field before constructing UI."""
        if (
            not isinstance(result, _SshLoginLookupResult)
            or result.endpoint != request.endpoint
            or not isinstance(result.status, _SshLoginLookupStatus)
            or (
                result.explicit_credential_id is not None
                and not isinstance(result.explicit_credential_id, UUID)
            )
            or (
                result.explicit_credential_generation is not None
                and not _is_valid_ssh_credential_generation(result.explicit_credential_generation)
            )
            or (
                (result.explicit_credential_id is None)
                != (result.explicit_credential_generation is None)
            )
            or any(
                not isinstance(credential, SshCredentialRef)
                or credential.endpoint != request.endpoint
                for credential in result.credentials
            )
        ):
            return False
        credential_ids = tuple(credential.credential_id for credential in result.credentials)
        if len(set(credential_ids)) != len(credential_ids):
            return False
        if result.status is not _SshLoginLookupStatus.READY:
            return (
                not result.credentials
                and result.explicit_credential_id is None
                and result.explicit_credential_generation is None
                and (
                    isinstance(result.guidance, str) and 0 < len(result.guidance) <= 4096
                    if result.status is _SshLoginLookupStatus.PREFLIGHT_REJECTED
                    else result.guidance is None
                )
            )
        if result.guidance is not None:
            return False
        if request.profile.ssh_account_id is not None:
            try:
                from termana.models.ssh_credential import validate_ssh_username
                validate_ssh_username(result.explicit_username)
            except (TypeError, ValueError):
                return False
            return not result.credentials and result.explicit_credential_id is not None
        if result.explicit_username is not None:
            return False
        if not request.is_saved_profile and request.profile.username is not None:
            return not result.credentials
        return result.explicit_credential_id is None

    def _apply_ssh_login_lookup_result(
        self,
        request: _SshLoginResolutionRequest,
        result: _SshLoginLookupResult,
    ) -> _ConnectionOpenState:
        """Present the existing chooser or manual terminal on the GUI thread."""
        connection_profile = (
            self._profile_without_ssh_username(request.profile, is_saved_profile=True)
            if request.is_saved_profile
            else request.profile
        )
        if result.status is _SshLoginLookupStatus.PREFLIGHT_REJECTED:
            self.statusBar().showMessage(result.guidance)
            return _ConnectionOpenState.REJECTED
        if request.profile.ssh_account_id is not None:
            if result.status is not _SshLoginLookupStatus.READY:
                return _ConnectionOpenState.REJECTED
            return self._open_resolved_ssh_lookup(
                request, _SshLoginResolution(
                    profile=replace(request.profile, username=result.explicit_username),
                    credential_id=result.explicit_credential_id,
                    credential_generation=result.explicit_credential_generation,
                ),
            )
        if result.status is _SshLoginLookupStatus.BUSY:
            return self._open_manual_ssh_login_after_lookup_failure(
                request,
                notice=tr("Saved logins are being updated; password entry remains manual."),
                preparation_completed=True,
            )
        if result.status is _SshLoginLookupStatus.UNAVAILABLE:
            return self._open_manual_ssh_login_after_lookup_failure(
                request,
                notice=tr(
                    "Saved login names could not be loaded; enter the login and password manually."
                ),
                preparation_completed=True,
            )
        if not request.is_saved_profile and request.profile.username is not None:
            return self._open_resolved_ssh_lookup(
                request,
                _SshLoginResolution(
                    profile=request.profile,
                    credential_id=result.explicit_credential_id,
                    credential_generation=result.explicit_credential_generation,
                ),
            )
        if not result.credentials:
            return self._open_resolved_ssh_lookup(
                request,
                _SshLoginResolution(profile=connection_profile),
            )

        dialog = SshLoginDialog(
            endpoint=_format_network_endpoint(request.profile.host, request.profile.port),
            options=tuple(
                SavedSshLoginOption(
                    credential_id=credential.credential_id,
                    username=credential.username,
                )
                for credential in result.credentials
            ),
            parent=self,
        )
        self._ssh_login_dialogs[request.request_id] = dialog
        removal = None
        if getattr(dialog, "remove_requested", None) is not None:
            from termana.ssh_login_removal import SshLoginRemovalController

            removal = SshLoginRemovalController(
                dialog,
                credentials=result.credentials,
                repository=request.repository,
                coordinator=self._ssh_credential_mutation_coordinator,
                owner_current=lambda: (
                    self._ssh_login_request_is_current(request)
                    and self._ssh_login_dialogs.get(request.request_id) is dialog
                ),
            )
            removal.status_message.connect(
                lambda message: self.statusBar().showMessage(
                    message, CONFIRMATION_MESSAGE_TIMEOUT_MS
                )
            )
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.statusBar().showMessage(
                    (removal.last_status if removal is not None else None)
                    or tr("SSH connection cancelled."),
                    PLACEHOLDER_MESSAGE_TIMEOUT_MS,
                )
                return _ConnectionOpenState.REJECTED
            selection = dialog.selection
            credentials = removal.credentials if removal is not None else result.credentials
        finally:
            if removal is not None:
                removal.stop()
            self._ssh_login_dialogs.pop(request.request_id, None)
            schedule_delete_later(dialog)
        if not self._ssh_login_request_is_current(request):
            return _ConnectionOpenState.REJECTED
        if not isinstance(selection, SshLoginSelection):
            LOGGER.warning("SSH login dialog returned an invalid selection")
            return _ConnectionOpenState.REJECTED

        selected_credential = next(
            (
                credential
                for credential in credentials
                if credential.credential_id == selection.credential_id
            ),
            None,
        )
        if selected_credential is not None and (
            selected_credential.endpoint != request.endpoint
            or selected_credential.username != selection.username
        ):
            LOGGER.warning("SSH login dialog returned inconsistent credential metadata")
            self.statusBar().showMessage(
                tr("The selected saved SSH login is no longer valid."),
                PLACEHOLDER_MESSAGE_TIMEOUT_MS,
            )
            return _ConnectionOpenState.REJECTED
        if selection.credential_id is not None and selected_credential is None:
            LOGGER.warning("SSH login dialog returned an unknown credential ID")
            self.statusBar().showMessage(
                tr("The selected saved SSH login is no longer available."),
                PLACEHOLDER_MESSAGE_TIMEOUT_MS,
            )
            return _ConnectionOpenState.REJECTED

        resolved_profile = (
            self._profile_with_ssh_username(
                request.profile,
                selection.username,
                is_saved_profile=request.is_saved_profile,
            )
            if selection.username is not None
            else self._profile_without_ssh_username(
                request.profile,
                is_saved_profile=request.is_saved_profile,
            )
        )
        preference_request = _SshPreferenceRequest(
            endpoint=request.endpoint,
            credential_id=(selection.credential_id if selection.remember_for_profile else None),
            profile_id=request.profile.profile_id if request.is_saved_profile else None,
        )
        return self._open_resolved_ssh_lookup(
            request,
            _SshLoginResolution(
                profile=resolved_profile,
                credential_id=selection.credential_id,
                credential_generation=(
                    selected_credential.generation if selected_credential is not None else None
                ),
                preference_request=preference_request,
            ),
        )

    def _open_manual_ssh_login_after_lookup_failure(
        self,
        request: _SshLoginResolutionRequest,
        *,
        notice: str,
        preparation_completed: bool = False,
    ) -> _ConnectionOpenState:
        """Keep Connect usable when a repository lookup cannot complete."""
        if request.embedded_known_hosts is not None and not preparation_completed:
            self.statusBar().showMessage(
                tr("Could not prepare SSH. No connection was started. Please try again."),
                PLACEHOLDER_MESSAGE_TIMEOUT_MS,
            )
            return _ConnectionOpenState.REJECTED
        profile = (
            self._profile_without_ssh_username(request.profile, is_saved_profile=True)
            if request.is_saved_profile
            else request.profile
        )
        return self._open_resolved_ssh_lookup(
            request,
            _SshLoginResolution(profile=profile, notice=notice),
        )

    def _open_resolved_ssh_lookup(
        self,
        request: _SshLoginResolutionRequest,
        resolution: _SshLoginResolution,
    ) -> _ConnectionOpenState:
        """Launch only while the immutable request still owns a valid UI context."""
        if not self._ssh_login_request_is_current(request):
            return _ConnectionOpenState.REJECTED
        return (
            _ConnectionOpenState.ACCEPTED
            if self._open_resolved_ssh_login(
                resolution,
                recovery_attempted=request.recovery_attempted,
                pane_id=self._ssh_login_request_pane_id(request),
                launch_guard=lambda: self._ssh_login_request_is_current(request),
                source_session_id=request.reconnect_session_id,
            )
            else _ConnectionOpenState.REJECTED
        )

    def _finish_ssh_connect_origin(
        self,
        request: _SshLoginResolutionRequest,
        *,
        succeeded: bool,
    ) -> None:
        """Resolve reconnect only after actual flow acceptance."""
        self._finish_immediate_ssh_connect_origin(
            origin=request.origin,
            reconnect_session_id=request.reconnect_session_id,
            succeeded=succeeded,
        )

    def _finish_immediate_ssh_connect_origin(
        self,
        *,
        origin: _SshConnectOrigin,
        reconnect_session_id: UUID | None,
        succeeded: bool,
    ) -> None:
        """Finish one initiating UI transaction after a non-pending launch result."""
        if origin is _SshConnectOrigin.RECONNECT:
            session_id = reconnect_session_id
            if session_id is not None:
                self.terminal_workspace.finish_terminal_reconnect(
                    session_id,
                    succeeded=succeeded,
                )

    @staticmethod
    def _profile_with_ssh_username(
        profile: ConnectionProfile,
        username: str,
        *,
        is_saved_profile: bool,
    ) -> ConnectionProfile:
        """Apply one selected username and keep unsaved session tab titles precise."""
        if is_saved_profile:
            return replace(profile, username=username)
        target = SshTarget(
            host=profile.host,
            port=profile.port,
            username=username,
        )
        return replace(
            profile,
            name=_bounded_session_name(format_ssh_target(target)),
            username=username,
        )

    @staticmethod
    def _profile_without_ssh_username(
        profile: ConnectionProfile,
        *,
        is_saved_profile: bool,
    ) -> ConnectionProfile:
        """Clear an implicit login while retaining a stable saved-profile title."""
        if is_saved_profile:
            return replace(profile, username=None)
        target = SshTarget(host=profile.host, port=profile.port)
        return replace(
            profile,
            name=_bounded_session_name(format_ssh_target(target)),
            username=None,
        )

    def _commit_ssh_preference(
        self,
        request: _SshPreferenceRequest | None,
    ) -> bool:
        """Persist chooser ordering after launch without blocking the GUI thread."""
        if request is None:
            return False
        repository = self._credential_repository
        if repository is None:
            return True

        def operation() -> object:
            repository.remember_login_reference(
                request.endpoint, request.credential_id, profile_id=request.profile_id
            )
            return None

        operation_id = uuid4()
        task = BackgroundOperation(operation_id, operation)
        context = _SshPreferenceOperationContext(
            request=request,
            repository=repository,
        )
        self._ssh_preference_operations[operation_id] = (task, context)
        task.signals.succeeded.connect(self._handle_ssh_preference_succeeded)
        task.signals.failed.connect(self._handle_ssh_preference_failed)
        try:
            self._credential_operation_starter(task)
        except Exception:
            if context.completed:
                return context.failed
            self._ssh_preference_operations.pop(operation_id, None)
            context.completed = True
            context.failed = True
            LOGGER.warning("The SSH login preference background operation could not start")
            self._resume_close_after_ssh_repository_operations()
            return True
        return context.failed if context.completed else False

    @Slot(object, object)
    def _handle_ssh_preference_succeeded(
        self,
        operation_id: object,
        result: object,
    ) -> None:
        """Consume one successful non-secret preference write."""
        entry = self._take_ssh_preference_operation(operation_id)
        if entry is None:
            return
        _task, context = entry
        context.completed = True
        if result is not None:
            context.failed = True
            LOGGER.warning("An SSH login preference write returned an invalid result")
            self._show_ssh_preference_error()
        self._resume_close_after_ssh_repository_operations()

    @Slot(object)
    def _handle_ssh_preference_failed(self, operation_id: object) -> None:
        """Report a preference-only failure without changing connection success."""
        entry = self._take_ssh_preference_operation(operation_id)
        if entry is None:
            return
        _task, context = entry
        context.completed = True
        context.failed = True
        LOGGER.warning("Could not remember the selected SSH login")
        self._show_ssh_preference_error()
        self._resume_close_after_ssh_repository_operations()

    def _take_ssh_preference_operation(
        self,
        operation_id: object,
    ) -> tuple[BackgroundOperation, _SshPreferenceOperationContext] | None:
        """Consume exactly one preference completion identified by UUID."""
        if not isinstance(operation_id, UUID):
            LOGGER.warning("An SSH login preference write returned an invalid identity")
            return None
        return self._ssh_preference_operations.pop(operation_id, None)

    def _show_ssh_preference_error(self) -> None:
        """Keep a successful terminal open after a retryable preference failure."""
        if not self._close_waiting_for_terminals:
            self.statusBar().showMessage(
                tr("Connected, but the login choice could not be remembered."),
                PLACEHOLDER_MESSAGE_TIMEOUT_MS,
            )

    def _resume_close_after_ssh_repository_operations(self) -> None:
        """Hold instance ownership until every possible index writer has finished."""
        if (
            self._close_waiting_for_terminals
            and not self._ssh_preference_operations
            and not self._ssh_login_resolution_operations
        ):
            QTimer.singleShot(0, self.close)

    @Slot(int)
    def _rdp_pending_count_changed(self, count):
        if count == 0:
            self._resume_close_after_ssh_repository_operations()

    def _open_telnet_connection(
        self,
        profile: ConnectionProfile,
        *,
        reconnect_session_id: UUID | None = None,
        pane_id: UUID | None = None,
    ) -> bool:
        """Confirm and open one unencrypted Telnet profile through its own backend."""
        if self._close_waiting_for_terminals or (
            reconnect_session_id is not None
            and (
                not isinstance(reconnect_session_id, UUID)
                or not self.terminal_workspace.terminal_reconnect_pending(reconnect_session_id)
            )
        ):
            return False
        if profile.protocol is not ConnectionProtocol.TELNET:
            self.statusBar().showMessage(tr("This connection protocol is not supported yet."))
            return False
        if pane_id is None:
            pane_id = (
                self.terminal_workspace.pane_id_for_session(reconnect_session_id)
                if reconnect_session_id is not None
                else self.terminal_workspace.current_pane_id
            )
        if pane_id is None or not self.terminal_workspace.pane_available(pane_id):
            return False
        if not self._confirm_telnet_connection(profile):
            if not self._close_waiting_for_terminals:
                self.statusBar().showMessage(
                    tr("Telnet connection canceled."),
                    CONFIRMATION_MESSAGE_TIMEOUT_MS,
                )
            return False
        if (
            self._close_waiting_for_terminals
            or not self.terminal_workspace.pane_available(pane_id)
            or (
                reconnect_session_id is not None
                and not self.terminal_workspace.terminal_reconnect_pending(reconnect_session_id)
            )
        ):
            return False

        try:
            backend = self._telnet_backend_factory(
                profile.host,
                profile.port,
                profile.telnet_encoding,
            )
        except ValueError as error:
            LOGGER.warning("Could not prepare Telnet backend: %s", error)
            self.statusBar().showMessage(
                tr("Could not start Telnet because the connection settings are invalid."),
                PLACEHOLDER_MESSAGE_TIMEOUT_MS,
            )
            return False
        terminal_session_id = self.terminal_workspace.open_backend_terminal_session(
            profile.name,
            backend,
            requires_direct_input=True,
            reconnect_profile=profile,
            pane_id=pane_id,
        )
        if terminal_session_id is None:
            self.statusBar().showMessage(
                tr("Interactive Telnet is not available right now."),
                PLACEHOLDER_MESSAGE_TIMEOUT_MS,
            )
            return False

        self.statusBar().showMessage(
            tr('Telnet terminal "{p0}" opened.', p0=f"{profile.name}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )
        return True

    def _handle_connection_reconnect_requested(
        self,
        session_id: object,
        profile: object,
    ) -> None:
        """Route an ended tab through the ordinary protocol and authentication flow."""
        if not isinstance(session_id, UUID):
            return
        if (
            self._close_waiting_for_terminals
            or not isinstance(profile, ConnectionProfile)
            or not self.terminal_workspace.terminal_reconnect_pending(session_id)
        ):
            self.terminal_workspace.finish_terminal_reconnect(
                session_id,
                succeeded=False,
            )
            return
        if profile.protocol is ConnectionProtocol.SSH:
            outcome = self._open_ssh_connection(
                profile,
                origin=_SshConnectOrigin.RECONNECT,
                reconnect_session_id=session_id,
            )
        elif profile.protocol is ConnectionProtocol.TELNET:
            outcome = (
                _ConnectionOpenState.ACCEPTED
                if self._open_telnet_connection(
                    profile,
                    reconnect_session_id=session_id,
                )
                else _ConnectionOpenState.REJECTED
            )
        else:
            outcome = self._open_connection(profile)
        if outcome is _ConnectionOpenState.PENDING:
            return
        if self.terminal_workspace.terminal_reconnect_pending(session_id):
            self.terminal_workspace.finish_terminal_reconnect(
                session_id,
                succeeded=bool(outcome),
            )

    def _confirm_telnet_connection(self, profile: ConnectionProfile) -> bool:
        """Require fresh consent unless this Telnet endpoint was explicitly remembered."""
        return confirm_telnet_connection(
            host=profile.host,
            port=profile.port,
            parent=self,
            consent_store=self._telnet_warning_store,
        )

    def _reset_telnet_warnings(self) -> None:
        try:
            self._telnet_warning_store.clear()
        except TelnetWarningStoreError:
            self.statusBar().showMessage(tr("Could not reset Telnet warning choices."), 8000)
        else:
            self.statusBar().showMessage(tr("Telnet warnings will be shown again."), 5000)

    def _handle_command_terminal_finished(
        self,
        session_id: object,
        recent_output: bytes,
        context: object,
        exit_code: int,
    ) -> None:
        """Explain SSH failures and offer guarded recovery for host-key conflicts."""
        diagnostics = b""
        approval_rejected = False
        if isinstance(context, _SshSessionContext) and context.authentication_id is not None:
            authentication = self._ssh_authentication_sessions.get(context.authentication_id)
            approval_rejected = bool(getattr(authentication, "host_key_approval_rejected", False))
            diagnostics = self._close_ssh_authentication(context.authentication_id)
            if isinstance(session_id, UUID) and diagnostics:
                self.terminal_workspace.append_terminal_diagnostic(session_id, diagnostics)
                recent_output = recent_output + b"\n" + diagnostics
        if approval_rejected and isinstance(session_id, UUID):
            self.terminal_workspace.show_connection_guidance(
                session_id,
                tr(
                    "The presented SSH key could not be matched to your approval. No password was sent. Verify the server identity before reconnecting; no additional key was accepted."
                ),
            )
            return
        if (
            not self._close_waiting_for_terminals
            and isinstance(session_id, UUID)
            and isinstance(context, _SshSessionContext)
        ):
            guidance = local_forward_failure_guidance(
                recent_output,
                context.profile.ssh_tunnels,
                exit_code,
                via_jump=context.profile.ssh_jump.mode is not SshJumpMode.DIRECT,
            )
            if guidance is None:
                guidance = ssh_connection_failure_guidance(
                    diagnostics,
                    exit_code,
                    via_jump=context.profile.ssh_jump.mode is not SshJumpMode.DIRECT,
                )
            if guidance is None:
                guidance = ssh_keepalive_timeout_guidance(
                    recent_output,
                    exit_code,
                    via_jump=context.profile.ssh_jump.mode is not SshJumpMode.DIRECT,
                )
            if guidance is not None:
                self.terminal_workspace.show_connection_guidance(session_id, guidance)
        if (
            self._close_waiting_for_terminals
            or exit_code != 255
            or not isinstance(session_id, UUID)
            or not isinstance(context, _SshSessionContext)
            or not self.terminal_workspace.contains_terminal_session(session_id)
        ):
            return

        # PTY output can be printed by the remote server. It must never supply
        # a fingerprint, path or missing record for a trust-file mutation.
        mismatch = None if context.recovery_attempted else parse_ssh_host_key_mismatch(diagnostics)
        if mismatch is None:
            if has_ssh_host_key_warning(recent_output):
                key_status = (
                    tr("No further key changes were made. ")
                    if context.recovery_attempted
                    else tr("No saved key was changed. ")
                )
                self.terminal_workspace.show_connection_guidance(
                    session_id,
                    tr("Automatic key recovery is unavailable for this host-key warning. ")
                    + key_status
                    + tr(
                        "Verify the server identity through a trusted channel and review your SSH configuration before reconnecting manually."
                    ),
                )
            return
        if context.profile.ssh_jump.mode is SshJumpMode.JUMP:
            # Gateway stderr and destination diagnostics share this output. A
            # matching subject is not proof of which SSH process emitted it.
            # Never mutate either host's trust based on combined route output.
            self._show_manual_host_key_warning(context.profile, mismatch)
            return
        if mismatch.received_fingerprint is None:
            self._show_unverifiable_host_key_warning(context.profile, mismatch)
            return
        expected_subject = known_hosts_target(
            context.profile.host,
            context.profile.port,
        )
        if (
            mismatch.host_key_subject.casefold() != expected_subject.casefold()
            or not self._known_hosts_updater.manages_path(mismatch.known_hosts_path)
        ):
            self._show_manual_host_key_warning(context.profile, mismatch)
            return
        try:
            approval = SshHostKeyApproval(
                SshEndpoint(context.profile.host, context.profile.port),
                mismatch.received_key_type,
                mismatch.received_fingerprint,
            )
        except (TypeError, ValueError):
            self._show_manual_host_key_warning(context.profile, mismatch)
            return
        if not self._confirm_ssh_host_key_replacement(context.profile, mismatch):
            return
        # The modal confirmation can process tab closure or application shutdown.
        # Recheck its owner before starting a mutation of the user's trust file.
        if (
            self._close_waiting_for_terminals
            or not self.terminal_workspace.contains_terminal_session(session_id)
            or not self.terminal_workspace.begin_host_key_recovery(session_id)
        ):
            return

        recovery = _SshHostKeyRecovery(
            failed_session_id=session_id,
            profile=context.profile,
            approval=approval,
            credential_id=context.credential_id,
            credential_generation=context.credential_generation,
        )
        self.statusBar().showMessage(tr("Checking the saved SSH host key..."))
        self.terminal_workspace.show_connection_guidance(
            session_id,
            tr("Updating the reviewed SSH host key. Your tab and terminal history stay open."),
        )
        self._known_hosts_updater.remove_host_key(
            recovery,
            host=context.profile.host,
            port=context.profile.port,
            line_number=mismatch.line_number,
            stored_key_type=mismatch.stored_key_type,
        )

    def _handle_command_terminal_started(
        self,
        session_id: object,
        context: object,
        process_id: int,
    ) -> None:
        """Bind AskPass to the exact process launched for this terminal tab."""
        if (
            not isinstance(session_id, UUID)
            or not isinstance(context, _SshSessionContext)
            or context.authentication_id is None
        ):
            return
        authentication = self._ssh_authentication_sessions.get(context.authentication_id)
        if authentication is None:
            return
        self._ssh_authentication_terminal_ids[context.authentication_id] = session_id
        try:
            authentication.bind_ssh_process(process_id)
        except (RuntimeError, TypeError, ValueError):
            LOGGER.warning("Could not bind secure SSH password handling to its process")
            self._close_ssh_authentication(
                context.authentication_id,
                collect_diagnostics=False,
            )
            QTimer.singleShot(
                0,
                lambda session_id=session_id: self.terminal_workspace.close_terminal_session(
                    session_id
                ),
            )

    def _handle_command_terminal_closing(
        self,
        _session_id: object,
        context: object,
    ) -> None:
        """Revoke AskPass before a manually closed terminal begins shutdown."""
        if not isinstance(context, _SshSessionContext) or context.authentication_id is None:
            return
        self._close_ssh_authentication(
            context.authentication_id,
            collect_diagnostics=False,
        )

    def _confirm_ssh_host_key_replacement(
        self,
        profile: ConnectionProfile,
        mismatch: SshHostKeyMismatch,
    ) -> bool:
        """Warn clearly before removing an obsolete local trust record."""
        endpoint = known_hosts_target(profile.host, profile.port)
        dialog = SshHostKeyDialog(
            endpoint=endpoint,
            received_key_type=mismatch.received_key_type or "UNKNOWN",
            received_fingerprint=mismatch.received_fingerprint or "Unavailable",
            stored_key_type=mismatch.stored_key_type,
            known_hosts_path=mismatch.known_hosts_path,
            line_number=mismatch.line_number,
            parent=self,
        )
        return self._exec_ssh_host_key_dialog(dialog) == QDialog.DialogCode.Accepted

    def _exec_ssh_host_key_dialog(self, dialog: QDialog) -> int:
        """Keep each nested host-key decision owned until its modal loop exits."""
        self._ssh_host_key_dialogs.add(dialog)
        try:
            if self._close_waiting_for_terminals:
                return QDialog.DialogCode.Rejected
            return dialog.exec()
        finally:
            self._ssh_host_key_dialogs.discard(dialog)
            schedule_delete_later(dialog)

    def _show_manual_host_key_warning(
        self,
        profile: ConnectionProfile,
        mismatch: SshHostKeyMismatch,
    ) -> None:
        """Explain why ambiguous host identity or trust files require inspection."""
        message_box = QMessageBox(self)
        message_box.setObjectName("manualSshHostKeyDialog")
        message_box.setWindowTitle(tr("Server identity changed"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        endpoint = known_hosts_target(profile.host, profile.port)
        route_details = ""
        if profile.ssh_jump.mode is SshJumpMode.JUMP:
            message_box.setText(tr("OpenSSH reported a changed host key on this connection route."))
            message_box.setInformativeText(
                tr(
                    "This connection uses a jump host. The warning may concern the gateway or the destination, so no saved keys will be changed here. Inspect the terminal output and verify the reported server and fingerprint through a trusted channel before reconnecting."
                )
            )
            route_details = tr(
                "Configured jump host: {p0}\n", p0=f"{profile.ssh_jump.proxy_jump_value}"
            )
        else:
            message_box.setText(tr('The SSH host key for "{p0}" has changed.', p0=f"{endpoint}"))
            message_box.setInformativeText(
                tr(
                    "Termana only performs automatic recovery when OpenSSH reports this exact endpoint in the standard user known_hosts file. SSH aliases, canonicalized hosts, and custom trust files require manual inspection. Verify the presented fingerprint through a trusted channel first."
                )
            )
        message_box.setDetailedText(
            tr(
                "{p0}Presented fingerprint: {p1}\nExpected target: {p2}\nReported target: {p3}\nReported file: {p4}\nOffending line: {p5}",
                p0=f"{route_details}",
                p1=f"{mismatch.received_fingerprint or 'Not reported by OpenSSH.'}",
                p2=f"{endpoint}",
                p3=f"{mismatch.host_key_subject}",
                p4=f"{mismatch.known_hosts_path}",
                p5=f"{mismatch.line_number}",
            )
        )
        self._exec_ssh_host_key_dialog(message_box)

    def _show_unverifiable_host_key_warning(
        self,
        profile: ConnectionProfile,
        mismatch: SshHostKeyMismatch,
    ) -> None:
        """Refuse automatic recovery when no fingerprint can be verified."""
        endpoint = known_hosts_target(profile.host, profile.port)
        message_box = QMessageBox(self)
        message_box.setObjectName("unverifiableSshHostKeyDialog")
        message_box.setWindowTitle(tr("Server identity changed"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(tr('The SSH host key for "{p0}" has changed.', p0=f"{endpoint}"))
        message_box.setInformativeText(
            tr(
                "OpenSSH did not provide a fingerprint that Termana could display. No saved key will be removed automatically. Inspect the terminal output and verify the server identity through a trusted channel."
            )
        )
        message_box.setDetailedText(
            tr(
                "known_hosts file: {p0}\nOffending line: {p1}",
                p0=f"{mismatch.known_hosts_path}",
                p1=f"{mismatch.line_number}",
            )
        )
        self._exec_ssh_host_key_dialog(message_box)

    def _handle_known_hosts_removal_finished(
        self,
        context: object,
        succeeded: bool,
        message: str,
    ) -> None:
        """Retry normal OpenSSH verification only after a guarded removal."""
        if self._close_waiting_for_terminals:
            self.close()
            return
        if not isinstance(context, _SshHostKeyRecovery):
            return
        if not succeeded:
            concise_message = message[:HOST_KEY_ERROR_MESSAGE_CHARACTERS]
            self.statusBar().showMessage(
                tr("Could not complete SSH host-key recovery: {p0}", p0=f"{concise_message}")
            )
            self.terminal_workspace.show_connection_guidance(
                context.failed_session_id,
                tr("Could not complete SSH host-key recovery: {p0}", p0=f"{concise_message}"),
            )
            self.terminal_workspace.finish_terminal_reconnect(
                context.failed_session_id, succeeded=False
            )
            return
        pane_id = self.terminal_workspace.pane_id_for_session(context.failed_session_id)
        if pane_id is None:
            self.statusBar().showMessage(
                tr(
                    "The saved SSH host-key entry is no longer present, but the closed terminal was not reopened."
                )
            )
            return
        if not self._start_resolved_ssh_connection(
            _SshLoginResolution(
                profile=context.profile,
                credential_id=context.credential_id,
                credential_generation=context.credential_generation,
            ),
            recovery_attempted=True,
            pane_id=pane_id,
            source_session_id=context.failed_session_id,
            host_key_approval=context.approval,
            launch_guard=lambda: self.terminal_workspace.contains_terminal_session(
                context.failed_session_id
            ),
        ):
            self.terminal_workspace.finish_terminal_reconnect(
                context.failed_session_id, succeeded=False
            )
            self.terminal_workspace.show_connection_guidance(
                context.failed_session_id,
                tr(
                    "The old key entry was removed, but SSH could not continue safely. The tab and history were kept. Verify the server identity before trying again."
                ),
            )
            return
        self.statusBar().showMessage(
            tr("Continuing SSH in the same tab with verification of the reviewed fingerprint."),
        )

    def _open_new_connection_dialog(self, _checked: bool = False) -> None:
        """Open a profile form targeting the User sessions root."""
        self._open_new_connection_in_folder(None)

    def _open_new_connection_in_folder(self, destination: object) -> None:
        """Open the profile form for one validated catalog destination."""
        if self._close_waiting_for_terminals:
            return
        is_valid, folder_id = self._resolve_connection_folder_id(destination)
        if not is_valid:
            return
        self._connection_profile_editor.create(folder_id)

    def _load_connection_catalog(self) -> None:
        """Load the stored tree without allowing a bad file to crash the UI."""
        active_transfer = self._connection_transfer_operation
        if (
            active_transfer is not None
            and active_transfer.kind is _ConnectionTransferKind.IMPORT_COMMIT
        ):
            self.statusBar().showMessage(tr("Wait for the imported connections to finish saving."))
            return
        if self._connection_store is None:
            return

        try:
            catalog = self._connection_store.load()
        except ConnectionStoreError as error:
            self._connection_store_ready = False
            self.connections_sidebar.set_catalog_available(False)
            self.new_connection_action.setEnabled(False)
            self.new_folder_action.setEnabled(False)
            self._update_connection_transfer_actions()
            self.statusBar().showMessage(tr("Could not load connections: {p0}", p0=f"{error}"))
            return

        self._connection_store_ready = True
        self._connection_catalog = catalog
        self.connections_sidebar.set_catalog(catalog)
        self.connections_sidebar.set_catalog_available(True)
        self.new_connection_action.setEnabled(True)
        self.new_folder_action.setEnabled(True)
        self._update_connection_transfer_actions()

        if catalog.profiles:
            self.statusBar().showMessage(
                tr("Loaded {p0} saved connection(s).", p0=f"{len(catalog.profiles)}"),
                CONFIRMATION_MESSAGE_TIMEOUT_MS,
            )

    def _open_connection_import_dialog(self, _checked: bool = False) -> None:
        """Select and parse one portable catalog without blocking the interface."""
        if (
            self._close_waiting_for_terminals
            or not self._connection_store_ready
            or self._connection_transfer_operation is not None
            or self._connection_transfer_confirmation_active
        ):
            return
        self._connection_transfer_confirmation_active = True
        self._update_connection_transfer_actions()
        try:
            selected_path = self._choose_connection_transfer_path(
                importing=True, title=tr("Import connections"), default_path=""
            )
        finally:
            self._connection_transfer_confirmation_active = False
            self._update_connection_transfer_actions()
        if not selected_path or self._close_waiting_for_terminals:
            return
        source_path = Path(selected_path)

        def import_connections(path: Path = source_path) -> object:
            payload = read_connection_transfer_bytes(path)
            return decode_connection_transfer(payload)

        self._start_connection_transfer_operation(
            kind=_ConnectionTransferKind.IMPORT,
            path=source_path,
            operation=import_connections,
        )

    def _open_connection_export_dialog(self, _checked: bool = False) -> None:
        """Export the entire catalog snapshot captured before the file picker."""
        self._export_connection_catalog(
            self._connection_catalog,
            scope_label=tr("All saved connections"),
            dialog_title=tr("Export all connections"),
        )

    def _open_connection_item_export_dialog(self, item_id: object) -> None:
        """Resolve one exact saved item; a stale target never falls back to all."""
        if not isinstance(item_id, UUID) or not self._connection_export_available():
            return
        catalog = self._connection_catalog
        item = catalog.folder_by_id(item_id) or catalog.profile_by_id(item_id)
        if item is None:
            self.statusBar().showMessage(tr("Could not find the connection or folder to export."))
            return
        is_folder = isinstance(item, ConnectionFolder)
        self._export_connection_catalog(
            catalog_for_connection_export(catalog, item_id),
            scope_label=f"{'Folder' if is_folder else 'Connection'} · {item.name}",
            dialog_title=tr("Export folder") if is_folder else tr("Export connection"),
            include_folder_contents=is_folder,
        )

    def _connection_export_available(self) -> bool:
        """Guard every export entry point, including requests from stale menus."""
        return bool(
            not self._close_waiting_for_terminals
            and self._connection_store_ready
            and self._connection_transfer_operation is None
            and not self._connection_transfer_confirmation_active
        )

    def _export_connection_catalog(
        self,
        catalog: ConnectionCatalog,
        *,
        scope_label: str,
        dialog_title: str,
        include_folder_contents: bool = False,
    ) -> None:
        """Confirm and export exactly the reviewed snapshot, never later additions."""
        if not self._connection_export_available() or not catalog.root_item_ids:
            return
        stats = ConnectionTransferStats(
            ssh_connections=sum(
                profile.protocol is ConnectionProtocol.SSH for profile in catalog.profiles
            ),
            telnet_connections=sum(
                profile.protocol is ConnectionProtocol.TELNET for profile in catalog.profiles
            ),
            folders=len(catalog.folders),
            rdp_connections=sum(profile.protocol is ConnectionProtocol.RDP for profile in catalog.profiles),
        )
        # QFileDialog also runs a nested event loop: hold the same reservation
        # as the following confirmation so a second transfer cannot slip in.
        self._connection_transfer_confirmation_active = True
        self._update_connection_transfer_actions()
        try:
            selected_path = self._choose_connection_transfer_path(
                importing=False,
                title=dialog_title,
                default_path=DEFAULT_CONNECTION_EXPORT_NAME,
            )
            if not selected_path or self._close_waiting_for_terminals:
                return
            connections_path = self._active_connection_store_path()
            try:
                prepared_target = prepare_connection_export_target(
                    Path(selected_path),
                    connections_path,
                )
            except ConnectionTransferPathError:
                self.statusBar().showMessage(
                    tr(
                        "Choose a regular file in an existing writable folder. Termana's working connections file cannot be used."
                    )
                )
                return
            destination_path = prepared_target.path
            try:
                export_confirmed = self._confirm_connection_transfer(
                    confirm_connection_export,
                    destination_path=destination_path,
                    stats=stats,
                    scope_label=scope_label,
                    include_folder_contents=include_folder_contents,
                    parent=self,
                )
            except Exception:
                self._show_connection_transfer_failure(_ConnectionTransferKind.EXPORT)
                return
            if self._close_waiting_for_terminals:
                return
            if not export_confirmed:
                self.statusBar().showMessage(
                    tr("Connection export canceled."),
                    CONFIRMATION_MESSAGE_TIMEOUT_MS,
                )
                return
        finally:
            self._connection_transfer_confirmation_active = False
            self._update_connection_transfer_actions()

        if not self._connection_export_available():
            return

        def export_connections(
            snapshot: ConnectionCatalog = catalog,
            target: PreparedConnectionExportTarget = prepared_target,
            exported_stats: ConnectionTransferStats = stats,
        ) -> object:
            payload = encode_connection_catalog(snapshot)
            write_prepared_connection_transfer_bytes(
                target,
                payload,
            )
            return exported_stats

        self._start_connection_transfer_operation(
            kind=_ConnectionTransferKind.EXPORT,
            path=destination_path,
            operation=export_connections,
        )

    def _active_connection_store_path(self) -> Path | None:
        """Return the real working data path when the configured store exposes one."""
        path = getattr(self._connection_store, "path", None)
        return path if isinstance(path, Path) else None

    def _register_connection_transfer_dialog(self, dialog: QDialog) -> bool:
        """Bind one modal decision to this live window before its nested loop."""
        if self._close_waiting_for_terminals or self._connection_transfer_dialog is not None:
            return False
        self._connection_transfer_dialog = dialog
        return True

    def _choose_connection_transfer_path(
        self, *, importing: bool, title: str, default_path: str
    ) -> str:
        """Own the native picker so accepted shutdown can reject it safely."""
        dialog = QFileDialog(self, title)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setObjectName(
            "importConnectionsFileDialog" if importing else "exportConnectionsFileDialog"
        )
        dialog.setAcceptMode(
            QFileDialog.AcceptMode.AcceptOpen if importing else QFileDialog.AcceptMode.AcceptSave
        )
        dialog.setFileMode(
            QFileDialog.FileMode.ExistingFile if importing else QFileDialog.FileMode.AnyFile
        )
        dialog.setNameFilter(tr(CONNECTION_TRANSFER_FILE_FILTER))
        if default_path:
            dialog.selectFile(default_path)
        try:
            if not self._register_connection_transfer_dialog(dialog):
                return ""
            if dialog.exec() != QDialog.DialogCode.Accepted or self._close_waiting_for_terminals:
                return ""
            paths = dialog.selectedFiles()
            return paths[0] if paths else ""
        finally:
            if self._connection_transfer_dialog is dialog:
                self._connection_transfer_dialog = None
            schedule_delete_later(dialog)

    def _confirm_connection_transfer(self, provider: Callable[..., bool], **values: object) -> bool:
        """Register built-in decisions without changing injected provider contracts."""
        if self._close_waiting_for_terminals:
            return False
        if provider in (
            transfer_dialogs_module.confirm_connection_import,
            transfer_dialogs_module.confirm_connection_export,
        ):
            values["dialog_created"] = self._register_connection_transfer_dialog
        previous_dialog = self._connection_transfer_dialog
        try:
            return provider(**values)
        finally:
            self._connection_transfer_dialog = previous_dialog

    def _start_connection_transfer_operation(
        self,
        *,
        kind: _ConnectionTransferKind,
        path: Path,
        operation: Callable[[], object],
        pending_import_commit: _PendingConnectionImportCommit | None = None,
    ) -> bool:
        """Start the only active transfer and retain it across queued completion."""
        if not isinstance(kind, _ConnectionTransferKind):
            raise TypeError("kind must be a connection transfer kind")
        if not isinstance(path, Path):
            raise TypeError("path must be a Path")
        if not callable(operation):
            raise TypeError("operation must be callable")
        if pending_import_commit is not None and not isinstance(
            pending_import_commit,
            _PendingConnectionImportCommit,
        ):
            raise TypeError("pending_import_commit must be an import commit snapshot")
        if (kind is _ConnectionTransferKind.IMPORT_COMMIT) != (pending_import_commit is not None):
            raise ValueError("pending_import_commit is required only for an import commit")
        if (
            self._close_waiting_for_terminals
            or self._connection_transfer_operation is not None
            or self._connection_transfer_confirmation_active
        ):
            return False

        operation_id = uuid4()
        task = BackgroundOperation(operation_id, operation)
        context = _ConnectionTransferOperationContext(
            operation_id=operation_id,
            kind=kind,
            path=path,
            task=task,
            pending_import_commit=pending_import_commit,
        )
        task.signals.succeeded.connect(self._handle_connection_transfer_succeeded)
        task.signals.failed.connect(self._handle_connection_transfer_failed)
        self._connection_transfer_operation = context
        self._connection_transfer_progress.setAccessibleName(
            tr("Importing connections")
            if kind is _ConnectionTransferKind.IMPORT
            else tr("Saving imported connections")
            if kind is _ConnectionTransferKind.IMPORT_COMMIT
            else tr("Exporting connections")
        )
        self._connection_transfer_progress.show()
        self.statusBar().showMessage(
            tr("Reading and checking the connection file…")
            if kind is _ConnectionTransferKind.IMPORT
            else tr("Saving imported connections…")
            if kind is _ConnectionTransferKind.IMPORT_COMMIT
            else tr("Writing the connection export…")
        )
        if kind is _ConnectionTransferKind.IMPORT_COMMIT:
            self.new_connection_action.setEnabled(False)
            self.new_folder_action.setEnabled(False)
            self.connections_sidebar.setEnabled(False)
        self._update_connection_transfer_actions()
        try:
            self._connection_transfer_operation_starter(task)
        except Exception:
            if self._connection_transfer_operation is not context:
                return True
            self._finish_connection_transfer_operation(operation_id)
            self._show_connection_transfer_failure(kind)
            return False
        return True

    @Slot(object, object)
    def _handle_connection_transfer_succeeded(
        self,
        operation_id: object,
        result: object,
    ) -> None:
        """Consume one exact transfer result and ignore duplicates or stale tasks."""
        context = self._finish_connection_transfer_operation(operation_id)
        if context is None:
            return
        if self._close_waiting_for_terminals:
            self.close()
            return
        if context.kind is _ConnectionTransferKind.IMPORT_COMMIT:
            pending = context.pending_import_commit
            if result is not True or pending is None:
                self._show_connection_transfer_failure(context.kind)
                return
            self._apply_connection_catalog(
                pending.catalog,
                select_item_id=pending.select_item_id,
            )
            self.statusBar().showMessage(
                self._connection_transfer_completion_message(
                    "Imported",
                    pending.stats,
                ),
                CONFIRMATION_MESSAGE_TIMEOUT_MS,
            )
            return
        if context.kind is _ConnectionTransferKind.EXPORT:
            if not isinstance(result, ConnectionTransferStats):
                self._show_connection_transfer_failure(context.kind)
                return
            self.statusBar().showMessage(
                self._connection_transfer_completion_message("Exported", result),
                CONFIRMATION_MESSAGE_TIMEOUT_MS,
            )
            return
        if not isinstance(result, DecodedConnectionTransfer):
            self._show_connection_transfer_failure(context.kind)
            return
        self._confirm_and_commit_connection_import(context.path, result)

    @Slot(object)
    def _handle_connection_transfer_failed(self, operation_id: object) -> None:
        """Reduce file, codec, and unexpected worker failures to a safe UI message."""
        context = self._finish_connection_transfer_operation(operation_id)
        if context is None:
            return
        if self._close_waiting_for_terminals:
            self.close()
            return
        self._show_connection_transfer_failure(context.kind)

    def _finish_connection_transfer_operation(
        self,
        operation_id: object,
    ) -> _ConnectionTransferOperationContext | None:
        """Release only the exact active operation and restore action availability."""
        context = self._connection_transfer_operation
        if (
            context is None
            or not isinstance(operation_id, UUID)
            or operation_id != context.operation_id
        ):
            return None
        self._connection_transfer_operation = None
        self._connection_transfer_progress.hide()
        if context.kind is _ConnectionTransferKind.IMPORT_COMMIT:
            self.connections_sidebar.setEnabled(True)
            self.new_connection_action.setEnabled(self._connection_store_ready)
            self.new_folder_action.setEnabled(self._connection_store_ready)
        self._update_connection_transfer_actions()
        return context

    def _confirm_and_commit_connection_import(
        self,
        source_path: Path,
        decoded: DecodedConnectionTransfer,
    ) -> None:
        """Merge against the latest catalog, preview exact counts, then persist once."""
        try:
            preview = merge_connection_transfer(self._connection_catalog, decoded)
        except ConnectionTransferError:
            self._show_connection_transfer_failure(_ConnectionTransferKind.IMPORT)
            return
        if preview.stats.total_items == 0:
            self.statusBar().showMessage(
                tr("The selected connection file is valid but contains no items."),
                CONFIRMATION_MESSAGE_TIMEOUT_MS,
            )
            return
        self._connection_transfer_confirmation_active = True
        self._update_connection_transfer_actions()
        import_confirmation_failed = False
        try:
            import_confirmed = self._confirm_connection_transfer(
                confirm_connection_import,
                source_path=source_path,
                stats=preview.stats,
                parent=self,
            )
        except Exception:
            import_confirmed = False
            import_confirmation_failed = True
        finally:
            self._connection_transfer_confirmation_active = False
            self._update_connection_transfer_actions()
        if import_confirmation_failed:
            self._show_connection_transfer_failure(_ConnectionTransferKind.IMPORT)
            return
        if not import_confirmed:
            self.statusBar().showMessage(
                tr("Connection import canceled."),
                CONFIRMATION_MESSAGE_TIMEOUT_MS,
            )
            return
        if self._close_waiting_for_terminals:
            return
        base_catalog = self._connection_catalog
        try:
            merged = merge_connection_transfer(base_catalog, decoded)
        except ConnectionTransferError:
            self._show_connection_transfer_failure(_ConnectionTransferKind.IMPORT)
            return
        first_imported_root_index = len(base_catalog.root_item_ids)
        select_item_id = merged.catalog.root_item_ids[first_imported_root_index]
        pending_commit = _PendingConnectionImportCommit(
            catalog=merged.catalog,
            stats=merged.stats,
            select_item_id=select_item_id,
        )
        store = self._connection_store
        if store is None:
            self._apply_connection_catalog(
                pending_commit.catalog,
                select_item_id=pending_commit.select_item_id,
            )
            self.statusBar().showMessage(
                self._connection_transfer_completion_message(
                    "Imported",
                    pending_commit.stats,
                ),
                CONFIRMATION_MESSAGE_TIMEOUT_MS,
            )
            return
        if not self._connection_store_ready:
            self.statusBar().showMessage(
                tr("Saving is disabled because the connection file could not be loaded.")
            )
            return

        def persist_import(
            selected_store: ConnectionStore = store,
            candidate: ConnectionCatalog = pending_commit.catalog,
        ) -> object:
            selected_store.save(candidate)
            return True

        self._start_connection_transfer_operation(
            kind=_ConnectionTransferKind.IMPORT_COMMIT,
            path=source_path,
            operation=persist_import,
            pending_import_commit=pending_commit,
        )

    def _show_connection_transfer_failure(self, kind: _ConnectionTransferKind) -> None:
        """Show actionable, non-sensitive failure text for one transfer direction."""
        if kind is _ConnectionTransferKind.IMPORT:
            message = tr(
                "Could not import connections. The selected file is unreadable, invalid, unsupported, or too large."
            )
        elif kind is _ConnectionTransferKind.IMPORT_COMMIT:
            message = tr(
                "Could not save the imported connections. Existing connections remain unchanged."
            )
        else:
            message = tr("Could not export connections. The selected destination was not written.")
        self.statusBar().showMessage(message)

    @staticmethod
    def _connection_transfer_completion_message(
        verb: str,
        stats: ConnectionTransferStats,
    ) -> str:
        """Return one compact status summary without exposing endpoint metadata."""
        if not isinstance(verb, str) or not verb:
            raise ValueError("verb must be non-empty text")
        if not isinstance(stats, ConnectionTransferStats):
            raise TypeError("stats must be ConnectionTransferStats")
        connection_noun = count_noun("connection", "connections", stats.connections)
        folder_noun = count_noun("folder", "folders", stats.folders)
        return tr(
            "{p0} {p1} {p2} and {p3} {p4}.",
            p0=tr(verb, context="connection-transfer"),
            p1=f"{stats.connections}",
            p2=f"{connection_noun}",
            p3=f"{stats.folders}",
            p4=f"{folder_noun}",
        )

    def _update_connection_transfer_actions(self) -> None:
        """Keep menu availability aligned with catalog safety and active work."""
        available = bool(
            self._connection_store_ready
            and not self._close_waiting_for_terminals
            and self._connection_transfer_operation is None
            and not self._connection_transfer_confirmation_active
        )
        self.import_connections_action.setEnabled(available)
        self.export_connections_action.setEnabled(
            available and bool(self._connection_catalog.root_item_ids)
        )
        self.save_ssh_session_action.setEnabled(
            self.terminal_workspace.current_ssh_session_id is not None
            and self._ssh_session_save_available()
        )

    def _ssh_session_save_available(self) -> bool:
        operation = self._connection_transfer_operation
        return (
            not self._close_waiting_for_terminals
            and (self._connection_store is None or self._connection_store_ready)
            and not (
                operation is not None and operation.kind is _ConnectionTransferKind.IMPORT_COMMIT
            )
        )

    def _add_connection_profile(
        self,
        profile: ConnectionProfile,
        *,
        parent_folder_id: UUID | None = None,
    ) -> bool:
        """Persist a profile first, then expose it through the interface."""
        try:
            candidate = self._connection_catalog.with_profile_added(
                profile,
                parent_folder_id=parent_folder_id,
            )
        except ValueError as error:
            self.statusBar().showMessage(tr("Could not add connection: {p0}", p0=f"{error}"))
            return False
        return self._commit_connection_catalog(
            candidate,
            select_item_id=profile.profile_id,
        )

    def _edit_connection_profile(self, profile: ConnectionProfile) -> None:
        """Edit a profile while preserving its identity and stored position."""
        if self._close_waiting_for_terminals:
            return
        self._connection_profile_editor.edit(profile)

    def _set_connection_favorite(self, profile_id: object, is_favorite: bool) -> None:
        """Persist a favorite mark against the current profile, never a stale row."""
        if (
            self._close_waiting_for_terminals
            or not isinstance(profile_id, UUID)
            or type(is_favorite) is not bool
        ):
            return
        profile = self._connection_catalog.profile_by_id(profile_id)
        if profile is None:
            self.statusBar().showMessage(tr("Could not find the connection to update."))
            return
        if profile.is_favorite == is_favorite:
            return
        candidate = self._connection_catalog.with_profile_updated(
            replace(profile, is_favorite=is_favorite)
        )
        if self._commit_connection_catalog(candidate):
            message = (
                tr("Connection added to favorites.")
                if is_favorite
                else tr("Connection removed from favorites.")
            )
            self.statusBar().showMessage(message, CONFIRMATION_MESSAGE_TIMEOUT_MS)

    def _duplicate_connection_profile(self, profile: object) -> None:
        """Persist a credential-free copy directly after one current saved profile."""
        if self._close_waiting_for_terminals or not isinstance(profile, ConnectionProfile):
            return
        current_profile = self._connection_catalog.profile_by_id(profile.profile_id)
        if current_profile is None:
            self.statusBar().showMessage(tr("Could not find the connection to duplicate."))
            return

        try:
            parent_folder_id = self._connection_catalog.parent_id_of(current_profile.profile_id)
            siblings = self._connection_catalog.children_of(parent_folder_id)
            source_index = next(
                index
                for index, sibling in enumerate(siblings)
                if isinstance(sibling, ConnectionProfile)
                and sibling.profile_id == current_profile.profile_id
            )
            duplicate_name = connection_profile_copy_name(
                current_profile.name,
                (sibling.name for sibling in siblings),
            )
            duplicate = ConnectionProfile(
                name=duplicate_name,
                host=current_profile.host,
                protocol=current_profile.protocol,
                port=current_profile.port,
                telnet_encoding=current_profile.telnet_encoding,
                word_delete_mode=current_profile.word_delete_mode,
                accent_color=current_profile.accent_color,
                ssh_cipher=current_profile.ssh_cipher,
                ssh_algorithms=current_profile.ssh_algorithms,
                ssh_options=current_profile.ssh_options,
                ssh_identity=current_profile.ssh_identity,
                ssh_jump=current_profile.ssh_jump,
                ssh_tunnels=current_profile.ssh_tunnels,
                rdp_options=current_profile.rdp_options.without_login(),
                # Favorites describe the user's chosen profiles, not settings
                # inherited by every copy of that endpoint.
                is_favorite=False,
                # A copied profile never inherits legacy login metadata. Saved
                # credentials remain endpoint-scoped and are not profile data.
                username=None,
            )
            candidate = self._connection_catalog.with_profile_added(
                duplicate,
                parent_folder_id=parent_folder_id,
                index=source_index + 1,
            )
        except (StopIteration, ValueError) as error:
            LOGGER.warning("Could not prepare connection duplicate: %s", error)
            self.statusBar().showMessage(tr("Could not duplicate this connection."))
            return

        if not self._commit_connection_catalog(
            candidate,
            select_item_id=duplicate.profile_id,
        ):
            return
        if not self.connections_sidebar.catalog_item_is_visible(duplicate.profile_id):
            self.statusBar().showMessage(
                tr(
                    'Connection "{p0}" duplicated. Clear the session filter to see it.',
                    p0=f"{duplicate.name}",
                ),
                CONFIRMATION_MESSAGE_TIMEOUT_MS,
            )
            return
        self.statusBar().showMessage(
            tr('Connection "{p0}" duplicated.', p0=f"{duplicate.name}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _delete_connection_profile(self, profile: ConnectionProfile) -> None:
        """Delete a profile only after explicit confirmation and persistence."""
        if self._close_waiting_for_terminals:
            return
        current_profile = self._connection_catalog.profile_by_id(profile.profile_id)
        if current_profile is None:
            self.statusBar().showMessage(tr("Could not find the connection to delete."))
            return

        if not self._confirm_connection_deletion(current_profile):
            return

        parent_folder_id = self._connection_catalog.parent_id_of(current_profile.profile_id)
        try:
            candidate = self._connection_catalog.without_profile(current_profile.profile_id)
        except ValueError as error:
            self.statusBar().showMessage(tr("Could not delete connection: {p0}", p0=f"{error}"))
            return
        if not self._commit_connection_catalog(
            candidate,
            select_item_id=parent_folder_id,
        ):
            return
        self.statusBar().showMessage(
            tr('Connection "{p0}" deleted.', p0=f"{current_profile.name}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _commit_connection_catalog(
        self,
        candidate: ConnectionCatalog,
        *,
        select_item_id: UUID | None = None,
    ) -> bool:
        """Persist a complete candidate before changing memory or the tree."""
        if not isinstance(candidate, ConnectionCatalog):
            raise TypeError("candidate must be a ConnectionCatalog")
        if self._close_waiting_for_terminals:
            # A modal editor can outlive a close request through its nested Qt
            # event loop.  Never let that late editor save an older snapshot
            # over a confirmed import that just finished during shutdown.
            return False
        active_transfer = self._connection_transfer_operation
        if (
            active_transfer is not None
            and active_transfer.kind is _ConnectionTransferKind.IMPORT_COMMIT
        ):
            self.statusBar().showMessage(tr("Wait for the imported connections to finish saving."))
            return False
        if self._connection_store is not None:
            if not self._connection_store_ready:
                self.statusBar().showMessage(
                    tr("Saving is disabled because the connection file could not be loaded.")
                )
                return False
            try:
                self._connection_store.save(candidate)
            except ConnectionStoreError as error:
                self.statusBar().showMessage(tr("Could not save connections: {p0}", p0=f"{error}"))
                return False

        self._apply_connection_catalog(candidate, select_item_id=select_item_id)
        return True

    def _apply_connection_catalog(
        self,
        candidate: ConnectionCatalog,
        *,
        select_item_id: UUID | None = None,
    ) -> None:
        """Publish a catalog only after its persistence transaction has succeeded."""
        if not isinstance(candidate, ConnectionCatalog):
            raise TypeError("candidate must be a ConnectionCatalog")
        if self._rdp_identity_repository is not None:
            self._rdp_identity_repository.catalog_changed(self._connection_catalog,candidate)
        self._connection_catalog = candidate
        self.terminal_workspace.update_connection_colors(candidate.profiles)
        self.connections_sidebar.set_catalog(
            candidate,
            select_item_id=select_item_id,
        )
        self._update_connection_transfer_actions()

    def _open_new_connection_folder(self, _checked: bool = False) -> None:
        """Open a folder form targeting the User sessions root."""
        self._open_new_connection_folder_in_parent(None)

    def _open_new_connection_folder_in_parent(self, parent: object) -> None:
        """Create one empty folder below a validated root or folder."""
        if self._close_waiting_for_terminals:
            return
        is_valid, parent_folder_id = self._resolve_connection_folder_id(parent)
        if not is_valid:
            return
        dialog = ConnectionFolderDialog(
            self,
            reserved_names=self._sibling_connection_folder_names(parent_folder_id),
        )
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            folder = dialog.connection_folder
        finally:
            schedule_delete_later(dialog)

        try:
            candidate = self._connection_catalog.with_folder_added(
                folder,
                parent_folder_id=parent_folder_id,
            )
        except ValueError as error:
            self.statusBar().showMessage(tr("Could not create folder: {p0}", p0=f"{error}"))
            return
        if not self._commit_connection_catalog(
            candidate,
            select_item_id=folder.folder_id,
        ):
            return
        self.statusBar().showMessage(
            tr('Folder "{p0}" created.', p0=f"{folder.name}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _rename_connection_folder(self, folder_id: object) -> None:
        """Rename one canonical folder without changing its descendants."""
        if self._close_waiting_for_terminals or not isinstance(folder_id, UUID):
            return
        folder = self._connection_catalog.folder_by_id(folder_id)
        if folder is None:
            self.statusBar().showMessage(tr("Could not find the folder to rename."))
            return
        parent_folder_id = self._connection_catalog.parent_id_of(folder.folder_id)
        dialog = ConnectionFolderDialog(
            self,
            folder=folder,
            reserved_names=self._sibling_connection_folder_names(parent_folder_id),
        )
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            renamed_folder = dialog.connection_folder
        finally:
            schedule_delete_later(dialog)
        if (
            renamed_folder.folder_id != folder.folder_id
            or renamed_folder.child_ids != folder.child_ids
        ):
            self.statusBar().showMessage(tr("Renamed folder returned unexpected contents."))
            return

        try:
            candidate = self._connection_catalog.with_folder_renamed(
                folder.folder_id,
                renamed_folder.name,
            )
        except ValueError as error:
            self.statusBar().showMessage(tr("Could not rename folder: {p0}", p0=f"{error}"))
            return
        if not self._commit_connection_catalog(
            candidate,
            select_item_id=folder.folder_id,
        ):
            return
        self.statusBar().showMessage(
            tr('Folder "{p0}" renamed.', p0=f"{renamed_folder.name}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _delete_connection_folder(self, folder_id: object) -> None:
        """Delete an explicitly confirmed empty folder only."""
        if self._close_waiting_for_terminals or not isinstance(folder_id, UUID):
            return
        folder = self._connection_catalog.folder_by_id(folder_id)
        if folder is None:
            self.statusBar().showMessage(tr("Could not find the folder to delete."))
            return
        if folder.child_ids:
            self.statusBar().showMessage(
                tr("This folder is not empty. Move or delete its contents first.")
            )
            return
        if not self._confirm_connection_folder_deletion(folder):
            return

        parent_folder_id = self._connection_catalog.parent_id_of(folder.folder_id)
        try:
            candidate = self._connection_catalog.without_empty_folder(folder.folder_id)
        except ValueError as error:
            self.statusBar().showMessage(tr("Could not delete folder: {p0}", p0=f"{error}"))
            return
        if not self._commit_connection_catalog(
            candidate,
            select_item_id=parent_folder_id,
        ):
            return
        self.statusBar().showMessage(
            tr('Folder "{p0}" deleted.', p0=f"{folder.name}"),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _move_connection_catalog_item(
        self,
        item_id: object,
        parent_folder_id: object,
        index: object,
    ) -> None:
        """Persist one sidebar move before reprojecting the catalog tree."""
        if self._close_waiting_for_terminals:
            return
        if not isinstance(item_id, UUID):
            self.statusBar().showMessage(tr("Could not move connection item: invalid item ID."))
            return
        if parent_folder_id is not None and not isinstance(parent_folder_id, UUID):
            self.statusBar().showMessage(
                tr("Could not move connection item: invalid destination folder ID.")
            )
            return
        if index is not None and type(index) is not int:
            self.statusBar().showMessage(
                tr("Could not move connection item: invalid destination position.")
            )
            return

        item = self._connection_catalog.folder_by_id(item_id)
        if item is None:
            item = self._connection_catalog.profile_by_id(item_id)
        if item is None:
            self.statusBar().showMessage(tr("Could not find the connection item to move."))
            return

        destination_folder = None
        if parent_folder_id is not None:
            destination_folder = self._connection_catalog.folder_by_id(parent_folder_id)
            if destination_folder is None:
                self.statusBar().showMessage(
                    tr("Could not find the destination connection folder.")
                )
                return

        current_parent_id = self._connection_catalog.parent_id_of(item_id)
        try:
            candidate = self._connection_catalog.with_item_moved(
                item_id,
                parent_folder_id=parent_folder_id,
                index=index,
            )
        except ValueError as error:
            self.statusBar().showMessage(tr("Could not move connection item: {p0}", p0=f"{error}"))
            return
        if candidate is self._connection_catalog:
            return
        if not self._commit_connection_catalog(candidate, select_item_id=item_id):
            return

        item_kind = tr("Folder") if isinstance(item, ConnectionFolder) else tr("Connection")
        destination = (
            tr("User sessions")
            if destination_folder is None
            else tr('folder "{p0}"', p0=f"{destination_folder.name}")
        )
        action = tr("reordered in") if current_parent_id == parent_folder_id else tr("moved to")
        self.statusBar().showMessage(
            f'{item_kind} "{item.name}" {action} {destination}.',
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _resolve_connection_folder_id(
        self,
        value: object,
    ) -> tuple[bool, UUID | None]:
        """Resolve one root/folder destination received from the passive sidebar."""
        if value is None:
            return True, None
        if isinstance(value, UUID) and self._connection_catalog.folder_by_id(value):
            return True, value
        self.statusBar().showMessage(tr("The selected connection folder no longer exists."))
        return False, None

    def _sibling_connection_folder_names(
        self,
        parent_folder_id: UUID | None,
    ) -> tuple[str, ...]:
        """Return sibling labels for inline duplicate validation in the dialog."""
        return tuple(
            item.name
            for item in self._connection_catalog.children_of(parent_folder_id)
            if isinstance(item, ConnectionFolder)
        )

    def _confirm_connection_folder_deletion(self, folder: ConnectionFolder) -> bool:
        """Ask before deleting an empty organizational folder."""
        message_box = QMessageBox(self)
        message_box.setObjectName("deleteConnectionFolderDialog")
        message_box.setWindowTitle(tr("Delete folder"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(tr('Delete the empty folder "{p0}"?', p0=f"{folder.name}"))
        message_box.setInformativeText(
            tr(
                "Only the folder will be removed. Saved connections are never deleted through this action."
            )
        )

        delete_button = QPushButton(tr("Delete folder"), message_box)
        delete_button.setObjectName("deleteConnectionFolderButton")
        message_box.addButton(delete_button, QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = QPushButton(tr("Cancel"), message_box)
        cancel_button.setObjectName("cancelConnectionFolderButton")
        message_box.addButton(cancel_button, QMessageBox.ButtonRole.RejectRole)
        message_box.setDefaultButton(cancel_button)
        message_box.setEscapeButton(cancel_button)

        try:
            message_box.exec()
            return message_box.clickedButton() is delete_button
        finally:
            schedule_delete_later(message_box)

    def _confirm_connection_deletion(self, profile: ConnectionProfile) -> bool:
        """Ask the user before permanently deleting a saved profile."""
        message_box = QMessageBox(self)
        message_box.setObjectName("deleteConnectionDialog")
        message_box.setWindowTitle(tr("Delete connection"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(tr('Delete "{p0}"?', p0=f"{profile.name}"))
        message_box.setInformativeText(
            tr("This removes the saved profile. This action cannot be undone.")
        )

        delete_button = QPushButton(tr("Delete"), message_box)
        delete_button.setObjectName("deleteConnectionButton")
        message_box.addButton(delete_button, QMessageBox.ButtonRole.DestructiveRole)

        cancel_button = QPushButton(tr("Cancel"), message_box)
        cancel_button.setObjectName("cancelDeleteButton")
        message_box.addButton(cancel_button, QMessageBox.ButtonRole.RejectRole)
        message_box.setDefaultButton(cancel_button)
        message_box.setEscapeButton(cancel_button)

        try:
            message_box.exec()
            return message_box.clickedButton() is delete_button
        finally:
            schedule_delete_later(message_box)

    def _open_settings_dialog(self, _checked: bool = False) -> None:
        """Open one modal editor and keep it open when persistence fails."""
        if self._close_waiting_for_terminals:
            return
        dialog = SettingsDialog(
            self,
            settings=self._settings_controller.settings,
        )
        dialog.save_requested.connect(
            lambda candidate: self._commit_settings_from_dialog(dialog, candidate)
        )
        dialog.theme_preview_requested.connect(self._settings_controller.preview_theme)
        try:
            dialog.exec()
        finally:
            self._settings_controller.clear_theme_preview()
            schedule_delete_later(dialog)

    def _open_saved_ssh_logins_dialog(self, _checked: bool = False) -> None:
        """Open the metadata-only manager without unlocking the credential store."""
        if self._close_waiting_for_terminals:
            return
        repository = self._credential_repository
        if repository is None:
            self.statusBar().showMessage(
                tr("Saved SSH logins are unavailable."),
                PLACEHOLDER_MESSAGE_TIMEOUT_MS,
            )
            return
        try:
            credentials = repository.saved_credentials()
        except SshCredentialRepositoryError:
            LOGGER.warning("Saved SSH login metadata could not be loaded")
            self._show_saved_ssh_logins_load_error()
            return

        dialog = SavedSshLoginsDialog(credentials=credentials, parent=self)
        dialog.remove_requested.connect(
            lambda credential_id: self._remove_saved_ssh_login_from_dialog(
                dialog,
                credential_id,
            )
        )
        dialog.remove_all_requested.connect(
            lambda credential_ids: self._remove_saved_ssh_logins_from_dialog(
                dialog,
                credential_ids,
            )
        )
        try:
            dialog.exec()
        finally:
            schedule_delete_later(dialog)

    def _remove_saved_ssh_login_from_dialog(
        self,
        dialog: SavedSshLoginsDialog,
        credential_id: object,
    ) -> None:
        """Start one opaque removal intent without blocking the GUI thread."""
        if self._close_waiting_for_terminals or not isinstance(credential_id, UUID):
            dialog.show_operation_error(
                tr("Termana couldn't identify the saved SSH login to remove.")
            )
            return
        repository = self._credential_repository
        if repository is None:
            dialog.show_operation_error(tr("Saved SSH logins are unavailable right now."))
            return
        self._start_saved_ssh_login_removal(
            dialog=dialog,
            repository=repository,
            credential_ids=frozenset({credential_id}),
            is_single=True,
        )

    def _remove_saved_ssh_logins_from_dialog(
        self,
        dialog: SavedSshLoginsDialog,
        credential_ids: object,
    ) -> None:
        """Start one all-or-nothing batch removal outside the GUI thread."""
        if (
            self._close_waiting_for_terminals
            or not isinstance(credential_ids, frozenset)
            or not credential_ids
            or any(not isinstance(item, UUID) for item in credential_ids)
        ):
            dialog.show_operation_error(
                tr("Termana couldn't identify the saved SSH logins to remove.")
            )
            return
        repository = self._credential_repository
        if repository is None:
            dialog.show_operation_error(tr("Saved SSH logins are unavailable right now."))
            return
        self._start_saved_ssh_login_removal(
            dialog=dialog,
            repository=repository,
            credential_ids=credential_ids,
            is_single=False,
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
        if self._ssh_credential_removal_operations:
            if not any(
                context.dialog is dialog
                for _task, context in self._ssh_credential_removal_operations.values()
            ):
                dialog.show_operation_error(
                    tr("Another saved SSH login change is already in progress.")
                )
            return
        dialog.clear_operation_error()
        self.saved_ssh_logins_action.setEnabled(False)
        operation_id = uuid4()
        if is_single:
            credential_id = next(iter(credential_ids))

            def operation() -> object:
                return repository.remove_credential(credential_id)

        else:

            def operation() -> object:
                return repository.remove_credentials(credential_ids)

        task = BackgroundOperation(operation_id, operation)
        context = _SshCredentialRemovalContext(
            dialog=dialog,
            repository=repository,
            credential_ids=credential_ids,
            is_single=is_single,
        )
        self._ssh_credential_removal_operations[operation_id] = (task, context)
        task.signals.succeeded.connect(self._handle_saved_ssh_login_removal_succeeded)
        task.signals.failed.connect(self._handle_saved_ssh_login_removal_failed)
        try:
            self._credential_operation_starter(task)
        except Exception:
            self._ssh_credential_removal_operations.pop(operation_id, None)
            self._update_saved_ssh_logins_action()
            LOGGER.warning("The saved SSH login background operation could not start")
            self._show_saved_ssh_login_removal_error(dialog)

    @Slot(object, object)
    def _handle_saved_ssh_login_removal_succeeded(
        self,
        operation_id: object,
        result: object,
    ) -> None:
        """Refresh metadata and release a pending dialog after a worker commit."""
        entry = self._take_saved_ssh_login_removal(operation_id)
        if entry is None:
            return
        _task, context = entry
        if context.is_single:
            if not isinstance(result, bool):
                LOGGER.warning("A saved SSH login removal returned an invalid result")
                self._show_saved_ssh_login_removal_error(context.dialog)
                self._resume_close_after_saved_ssh_login_removal(context.dialog)
                return
            removed_count = int(result)
        else:
            if not isinstance(result, tuple) or any(
                not isinstance(credential, SshCredentialRef) for credential in result
            ):
                LOGGER.warning("A saved SSH login batch removal returned an invalid result")
                self._show_saved_ssh_login_removal_error(context.dialog)
                self._resume_close_after_saved_ssh_login_removal(context.dialog)
                return
            returned_ids = frozenset(credential.credential_id for credential in result)
            if len(returned_ids) != len(result) or not returned_ids <= context.credential_ids:
                LOGGER.warning("A saved SSH login batch removal returned inconsistent metadata")
                self._show_saved_ssh_login_removal_error(context.dialog)
                self._resume_close_after_saved_ssh_login_removal(context.dialog)
                return
            removed_count = len(result)

        if not self._refresh_saved_ssh_logins_dialog(
            context.dialog,
            context.repository,
        ):
            self._resume_close_after_saved_ssh_login_removal(context.dialog)
            return
        requested_count = len(context.credential_ids)
        if context.is_single:
            message = (
                tr("Saved SSH login removed.")
                if removed_count
                else tr("That saved SSH login had already been removed.")
            )
        elif removed_count == requested_count:
            message = (
                tr("Saved SSH login removed.")
                if removed_count == 1
                else tr("{p0} saved SSH logins removed.", p0=f"{removed_count}")
            )
        elif removed_count == 0:
            message = tr("Those saved SSH logins had already been removed.")
        else:
            removed_label = tr("saved SSH login") if removed_count == 1 else tr("saved SSH logins")
            missing_count = requested_count - removed_count
            missing_label = (
                tr("saved SSH login was") if missing_count == 1 else tr("saved SSH logins were")
            )
            message = tr(
                "{p0} {p1} removed; {p2} {p3} already absent.",
                p0=f"{removed_count}",
                p1=f"{removed_label}",
                p2=f"{missing_count}",
                p3=f"{missing_label}",
            )
        self.statusBar().showMessage(
            message,
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )
        self._resume_close_after_saved_ssh_login_removal(context.dialog)

    @Slot(object)
    def _handle_saved_ssh_login_removal_failed(self, operation_id: object) -> None:
        """Release a pending dialog after a private worker failure."""
        entry = self._take_saved_ssh_login_removal(operation_id)
        if entry is None:
            return
        _task, context = entry
        LOGGER.warning("A saved SSH login background removal failed")
        self._show_saved_ssh_login_removal_error(context.dialog)
        self._resume_close_after_saved_ssh_login_removal(context.dialog)

    def _take_saved_ssh_login_removal(
        self,
        operation_id: object,
    ) -> tuple[BackgroundOperation, _SshCredentialRemovalContext] | None:
        """Consume exactly one queued completion identified by an opaque UUID."""
        if not isinstance(operation_id, UUID):
            LOGGER.warning("A saved SSH login operation returned an invalid identity")
            return None
        entry = self._ssh_credential_removal_operations.pop(operation_id, None)
        self._update_saved_ssh_logins_action()
        return entry

    def _update_saved_ssh_logins_action(self) -> None:
        """Expose the manager only when its repository and worker are available."""
        self.saved_ssh_logins_action.setEnabled(
            self._credential_repository is not None
            and not self._ssh_credential_removal_operations
            and not self._close_waiting_for_terminals
        )

    def _resume_close_after_saved_ssh_login_removal(
        self,
        dialog: SavedSshLoginsDialog,
    ) -> None:
        """Let an application close continue only after its keychain transaction ends."""
        if not self._close_waiting_for_terminals:
            return
        dialog.reject()
        if not self._ssh_credential_removal_operations:
            QTimer.singleShot(0, self.close)

    def _refresh_saved_ssh_logins_dialog(
        self,
        dialog: SavedSshLoginsDialog,
        repository: SshCredentialRepository,
    ) -> bool:
        """Refresh only non-secret rows after a successful repository commit."""
        try:
            credentials = repository.saved_credentials()
        except SshCredentialRepositoryError:
            LOGGER.warning("Saved SSH login metadata could not be refreshed")
            dialog.show_operation_error(
                tr(
                    "The change was saved, but the list couldn't be refreshed. Close and reopen this window to try again."
                )
            )
            return False
        dialog.set_credentials(credentials)
        return True

    def _show_saved_ssh_login_removal_error(
        self,
        dialog: SavedSshLoginsDialog,
    ) -> None:
        """Report a retryable credential-store failure without exposing internals."""
        message = tr(
            "Termana couldn't finish removing the saved SSH login. Unlock or check your system credential store, then try again."
        )
        dialog.show_operation_error(message)
        self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)

    def _show_saved_ssh_logins_load_error(self) -> None:
        """Distinguish an unreadable metadata index from a genuinely empty list."""
        message_box = QMessageBox(self)
        message_box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        message_box.setObjectName("savedSshLoginsLoadErrorDialog")
        message_box.setWindowTitle(tr("Saved SSH logins unavailable"))
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(tr("Termana couldn't load the saved SSH login list."))
        message_box.setInformativeText(
            tr("Your saved passwords were not read or changed. Close this message and try again.")
        )
        message_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        try:
            message_box.exec()
        finally:
            schedule_delete_later(message_box)

    def _commit_settings_from_dialog(
        self,
        dialog: SettingsDialog,
        candidate: ApplicationSettings,
    ) -> None:
        """Persist one candidate before applying it and closing the editor."""
        try:
            self._settings_controller.commit(candidate)
        except SettingsStoreError as error:
            message = tr("Could not save settings: {p0}", p0=f"{error}")
            dialog.show_save_error(message)
            self.statusBar().showMessage(message)
            return

        dialog.accept()
        self.statusBar().showMessage(
            tr("Settings saved and applied."),
            CONFIRMATION_MESSAGE_TIMEOUT_MS,
        )

    def _show_update_dialog(self, _checked=False):
        from termana.updates.ui import UpdateDialog
        dialog = getattr(self, "_update_dialog", None)
        if dialog is None:
            dialog = self._update_dialog = UpdateDialog(self)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.check()

    def _show_about_dialog(self, _checked: bool = False) -> None:
        """Show the application identity and required GPL legal notice."""
        message_box = QMessageBox(self)
        message_box.setObjectName("aboutTermanaDialog")
        message_box.setWindowTitle(tr("About {p0}", p0=f"{APP_NAME}"))
        message_box.setIcon(QMessageBox.Icon.Information)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setText(version_label())
        message_box.setInformativeText(
            support_details().split("\n", 1)[1]
            + "\n\n"
            + tr(
                "A free desktop terminal application.\n\nCopyright © 2026 Termana contributors.\nLicensed under the GNU General Public License, version 3 only.\nThis program comes with absolutely no warranty.\n\nSee the LICENSE file distributed with Termana or visit https://www.gnu.org/licenses/gpl-3.0.html for the complete terms."
            )
        )
        message_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        copy_button = message_box.addButton(tr("Copy details"), QMessageBox.ButtonRole.ActionRole)
        copy_button.setObjectName("copyBuildDetailsButton")
        copy_button.setAutoDefault(False)
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(support_details()))

        try:
            message_box.exec()
        finally:
            schedule_delete_later(message_box)

    def _ssh_account_choices(self):
        repository = self._credential_repository
        if repository is None:
            return ()
        try:
            return repository.accounts()
        except SshCredentialRepositoryError:
            self.statusBar().showMessage("SSH accounts are unavailable; existing bindings are retained.")
            return ()

    def _manage_ssh_accounts(self, _checked=False):
        from termana.dialogs.ssh_accounts_dialog import SshAccountsDialog

        if self._credential_repository is None or self._close_waiting_for_terminals:
            return None
        if self._ssh_account_dialog is not None:
            self._ssh_account_dialog.raise_()
            self._ssh_account_dialog.activateWindow()
            return None
        dialog = None
        try:
            dialog = SshAccountsDialog(self._credential_repository,
                                      self._ssh_credential_mutation_coordinator, self)
            self._ssh_account_dialog = dialog
            dialog.exec()
        except SshCredentialRepositoryError:
            self.statusBar().showMessage("SSH accounts could not be loaded.")
        finally:
            self._ssh_account_dialog = None
            if dialog is not None:
                schedule_delete_later(dialog)
        return self._ssh_account_choices()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Keep Qt alive while terminal and known_hosts helpers stop."""
        if not self._close_waiting_for_terminals and not self._application_close_confirmed:
            active_terminal_count = self.terminal_workspace.active_terminal_count
            if active_terminal_count and self.isVisible():
                if not self._confirm_application_close(active_terminal_count):
                    event.ignore()
                    return
            self._application_close_confirmed = True
        self._persist_window_layout_once()
        self._close_waiting_for_terminals = True
        if self._ssh_account_dialog is not None:
            self._ssh_account_dialog.reject()
        if self._private_recovery_dialog is not None and isValid(self._private_recovery_dialog):
            # Its closeEvent dismisses owned pickers and releases recovery leases.
            self._private_recovery_dialog.close()
        if self._connection_transfer_dialog is not None:
            # End unconfirmed transfer loops before disabling their parent. An
            # already authorized background write remains owned and awaited below.
            self._connection_transfer_dialog.reject()
        for dialog in tuple(self._ssh_login_dialogs.values()):
            # Lookup has already completed, but its chooser still owns a modal
            # loop. End every owned chooser before disabling the parent window.
            dialog.reject()
        for dialog in tuple(self._ssh_host_key_dialogs):
            # Warnings and replacement decisions can be nested when SSH exits
            # arrive together. None may outlive accepted application shutdown.
            dialog.reject()
        if self._ssh_tunnel_launch_dialog is not None:
            # End the nested confirmation loop before disabling its parent;
            # otherwise a disabled modal window can keep a closed app alive.
            self._ssh_tunnel_launch_dialog.reject()
        self._terminal_output_controller.stop_accepting_exports()
        self._ssh_config_controller.stop()
        self._gateway_import_controller.stop()
        self._group_input_controller.stop()
        self._command_snippet_controller.stop()
        self._open_terminals_controller.stop()
        self._terminal_multi_search_controller.stop()
        self._ssh_session_details_controller.stop()
        self._sftp_controller.stop()
        self._ssh_session_save_controller.stop()
        self._connection_profile_editor.stop()
        self._update_connection_transfer_actions()
        self.terminal_workspace.suspend_shortcut_fallback()
        active_ssh_lookup = self._active_ssh_login_resolution()
        if active_ssh_lookup is not None:
            self._abandon_ssh_login_resolution(active_ssh_lookup)
        self._set_ssh_login_lookup_pending(None)
        self.setEnabled(False)
        if self._ssh_credential_removal_operations:
            self.statusBar().showMessage(tr("Finishing the saved SSH login change before closing…"))
            event.ignore()
            return
        if not self._terminal_shutdown_complete:
            if not self.terminal_workspace.shutdown_all():
                if self._known_hosts_updater.is_running:
                    self._known_hosts_updater.cancel()
                event.ignore()
                return
            self._terminal_shutdown_complete = True
        transfer_operation = self._connection_transfer_operation
        if transfer_operation is not None:
            message = (
                tr("Finishing the connection file check before closing…")
                if transfer_operation.kind is _ConnectionTransferKind.IMPORT
                else tr("Finishing the imported connection save before closing…")
                if transfer_operation.kind is _ConnectionTransferKind.IMPORT_COMMIT
                else tr("Finishing the confirmed connection export before closing…")
            )
            self.statusBar().showMessage(message)
            event.ignore()
            return
        if self._rdp_identity_repository is not None and self._rdp_identity_repository.is_busy:
            self.statusBar().showMessage(tr("Finishing the RDP system-vault request before closing…"))
            event.ignore()
            return
        if self._ssh_credential_mutation_coordinator.has_pending_operations:
            self.statusBar().showMessage(
                tr("Finishing the secure SSH credential change before closing…")
            )
            event.ignore()
            return
        if self._ssh_preference_operations or self._ssh_login_resolution_operations:
            # Login lookup can prune stale references as well as read the index.
            # Tombstoning its UI result does not cancel that repository write.
            self.statusBar().showMessage(
                tr("Finishing the saved SSH login check or preference change…")
            )
            event.ignore()
            return
        if self._terminal_output_controller.is_busy:
            self.statusBar().showMessage(tr("Finishing the terminal output export before closing…"))
            event.ignore()
            return
        if self._known_hosts_updater.is_running:
            self._known_hosts_updater.cancel()
            if self._known_hosts_updater.is_running:
                event.ignore()
                return
        super().closeEvent(event)

    def _confirm_application_close(self, active_terminal_count: int) -> bool:
        """Ask once before an application close interrupts live terminal processes."""
        if type(active_terminal_count) is not int or active_terminal_count <= 0:
            raise ValueError("active_terminal_count must be a positive integer")
        noun = tr("terminal") if active_terminal_count == 1 else tr("terminals")
        dialog = ActionDialog(
            object_name="closeApplicationDialog",
            window_title=tr("Close Termana?"),
            title=tr("Close {p0} active {p1}?", p0=f"{active_terminal_count}", p1=f"{noun}"),
            subtitle=tr("Active terminal sessions are still running"),
            body=(
                tr("Closing Termana will stop these sessions and any commands running inside them.")
            ),
            primary_text=tr("Close {p0}", p0=f"{noun}"),
            primary_object_name="confirmCloseApplicationButton",
            secondary_text=tr("Keep open"),
            secondary_object_name="cancelCloseApplicationButton",
            tone="notice",
            primary_role="danger",
            badge_text="!",
            parent=self,
        )
        try:
            return dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            schedule_delete_later(dialog)

    def _finish_close_after_terminal_shutdown(self) -> None:
        """Retry a pending window close after the final backend has stopped."""
        if not self._close_waiting_for_terminals:
            return
        if not self.terminal_workspace.shutdown_all():
            return
        self._terminal_shutdown_complete = True
        self.close()
