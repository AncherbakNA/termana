"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow

if TYPE_CHECKING:
    from termana.ssh.embedded_ssh_backend import EmbeddedSshBackend
    from termana.ssh.embedded_ssh_session import EmbeddedSshAuthenticationSession
from collections.abc import Callable
from uuid import UUID, uuid4

from termana.app.paths import known_hosts_file_path
from termana.app.window_contracts import (
    CONFIRMATION_MESSAGE_TIMEOUT_MS,
    LOGGER,
    PLACEHOLDER_MESSAGE_TIMEOUT_MS,
    SYSTEM_SSH_UNAVAILABLE_GUIDANCE,
    _ConnectionOpenState,
    _SshConnectOrigin,
    _SshLoginPromptContext,
    _SshLoginResolution,
    _SshSessionContext,
)
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.models.ssh_credential import SshEndpoint
from termana.ssh.dialogs.ssh_tunnel_launch_dialog import (
    SshTunnelLaunchDialog,
    confirm_ssh_tunnel_launch,
)
from termana.ssh.embedded_preflight import embedded_preparation_guidance, embedded_route_guidance
from termana.ssh.host_key_approval import SshHostKeyApproval
from termana.ssh.ssh_auth import SshAuthenticationSession
from termana.ssh.ssh_command import build_ssh_command
from termana.ui.i18n import tr


def _open_ssh_connection(
    self: MainWindow,
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
            origin=origin, reconnect_session_id=reconnect_session_id, succeeded=False
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
    self: MainWindow,
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
                tr("Could not open the SSH login prompt."), PLACEHOLDER_MESSAGE_TIMEOUT_MS
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


def _check_ssh_profile_available(self: MainWindow, profile, prompt_session_id=None):
    if profile.ssh_algorithms.protocol != "1":
        if self._embedded_ssh_known_hosts is not None:
            guidance = embedded_route_guidance(profile)
            if guidance is not None:
                if prompt_session_id is not None:
                    self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, guidance)
                self.statusBar().showMessage(guidance)
                return False
        return self._check_system_ssh_available(prompt_session_id)
    from termana.ssh.legacy_ssh import legacy_helper_path, verify_legacy_frontend
    from termana.ssh.legacy_ssh_backend import validate_legacy_profile

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


def _check_system_ssh_available(self: MainWindow, prompt_session_id: UUID | None = None) -> bool:
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
    self: MainWindow,
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
    if launch_guard is not None and (not launch_guard()):
        return False
    if not self._check_ssh_profile_available(profile, prompt_session_id):
        return False
    if profile.username is None:
        raise ValueError("A resolved SSH launch requires a username")
    if host_key_approval is not None and (
        source_session_id is None
        or self._embedded_ssh_known_hosts is not None
        or host_key_approval.endpoint != SshEndpoint(profile.host, profile.port)
        or (not self.terminal_workspace.terminal_reconnect_pending(source_session_id))
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
        from termana.ssh.dialogs.legacy_ssh_dialog import confirm_legacy_ssh

        snapshot = self._connection_catalog.profile_by_id(profile.profile_id)

        def legacy_owner_live():
            return (
                not self._close_waiting_for_terminals
                and (launch_guard is None or launch_guard())
                and self.terminal_workspace.pane_available(pane_id)
                and (
                    owner_session_id is None
                    or self.terminal_workspace.pane_id_for_session(owner_session_id) == pane_id
                )
                and (self._connection_catalog.profile_by_id(profile.profile_id) == snapshot)
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
            message = (
                tr("The SSH1 component could not be prepared safely. No connection was opened.")
                if profile.ssh_algorithms.protocol == "1"
                else embedded_preparation_guidance(error)
            )
            if prompt_session_id is not None:
                self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, message)
            self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)
            return False
        command = None
    else:
        if not self._check_system_ssh_available(prompt_session_id):
            return False
        try:
            system_config = (
                None
                if self._system_ssh_config_store is None
                else self._system_ssh_config_store.prepare()
            )
        except (OSError, RuntimeError, ValueError):
            message = tr(
                "Termana SSH config could not be verified safely. Review ~/.termana/ssh_config and folder permissions; no SSH connection was opened."
            )
            if prompt_session_id is not None:
                self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, message)
            self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)
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
            known_hosts=known_hosts_file_path()
            if self._system_ssh_config_store is None
            else self._system_ssh_config_store.trust,
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
                self._close_ssh_authentication(authentication_id, collect_diagnostics=False)
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
    if self._close_waiting_for_terminals or (launch_guard is not None and (not launch_guard())):
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
            terminal_session_id = self.terminal_workspace.replace_ended_command_terminal_session(
                source_session_id, command, session_context
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
        LOGGER.warning("Could not start the SSH terminal")
        if authentication_id is not None:
            self._close_ssh_authentication(authentication_id, collect_diagnostics=False)
        message = tr("Could not start SSH. Please try again.")
        if prompt_session_id is not None:
            self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, message)
        self.statusBar().showMessage(message, PLACEHOLDER_MESSAGE_TIMEOUT_MS)
        return False
    if terminal_session_id is None:
        if authentication_id is not None:
            self._close_ssh_authentication(authentication_id, collect_diagnostics=False)
        message = tr("Interactive SSH is not available on this platform yet.")
        if prompt_session_id is not None:
            message = tr("Could not start SSH. Check the login and try again.")
            self.terminal_workspace.show_ssh_login_prompt_error(prompt_session_id, message)
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
    self: MainWindow,
    profile: ConnectionProfile,
    resolved_login: _SshLoginResolution,
    *,
    tunnels_confirmed: bool,
) -> tuple[EmbeddedSshBackend, EmbeddedSshAuthenticationSession]:
    """Prepare the primary engine without changing routing or falling back."""
    from termana.ssh.embedded_ssh import EmbeddedSshPlan
    from termana.ssh.embedded_ssh_auth import require_embedded_auth_contract
    from termana.ssh.embedded_ssh_backend import EmbeddedSshBackend
    from termana.ssh.embedded_ssh_session import EmbeddedSshAuthenticationSession

    authentication_id = uuid4()
    if profile.ssh_algorithms.protocol == "1":
        from termana.ssh.legacy_ssh import legacy_helper_path
        from termana.ssh.legacy_ssh_backend import LegacyConnectionPlan, LegacySshBackend

        trust = (self._embedded_ssh_known_hosts or known_hosts_file_path()).with_name(
            "ssh1_known_hosts.json"
        )
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
            and (session_id is not None)
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
    return (backend, authentication)


def _confirm_current_ssh_tunnels(
    self: MainWindow,
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
            or (not self.terminal_workspace.pane_available(current_pane_id))
            or (self._credential_repository is not repository)
            or (self._connection_store is not store)
            or (self._ssh_program != ssh_program)
            or (launch_guard is not None and (not launch_guard()))
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
                and (now[0] is submission[0])
                and (now[1] == submission[1] == profile.username)
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
                self._embedded_ssh_known_hosts is None and self._system_ssh_config_store is not None
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
    self: MainWindow, session_id: object, context: object, username: str
) -> None:
    """Promote one terminal-owned ``login as`` response into an SSH process."""
    if (
        self._close_waiting_for_terminals
        or not isinstance(session_id, UUID)
        or (not isinstance(context, _SshLoginPromptContext))
    ):
        return
    is_saved_profile = (
        self._connection_catalog.profile_by_id(context.profile.profile_id) is not None
    )
    try:
        profile = self._profile_with_ssh_username(
            context.profile, username, is_saved_profile=is_saved_profile
        )
    except ValueError:
        self.terminal_workspace.show_ssh_login_prompt_error(
            session_id, tr("Enter a valid SSH username.")
        )
        return
    self._start_resolved_ssh_connection(
        _SshLoginResolution(
            profile=profile, preference_request=context.preference_request, notice=context.notice
        ),
        recovery_attempted=context.recovery_attempted,
        prompt_session_id=session_id,
    )


def _create_ssh_authentication(
    self: MainWindow, profile: ConnectionProfile, resolved_login: _SshLoginResolution
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
            credential_mutation_coordinator=self._ssh_credential_mutation_coordinator,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        LOGGER.warning("Secure SSH password handling is unavailable")
        return None
