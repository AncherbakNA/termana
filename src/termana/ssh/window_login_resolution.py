"""Window integration functions; the existing MainWindow owns all Qt/state.

Bodies retain original validation, signal order and asynchronous guards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from termana.app.main_window import MainWindow
from dataclasses import replace
from uuid import UUID, uuid4

from PySide6.QtWidgets import QDialog

from termana.app.background_operation import BackgroundOperation
from termana.app.qt_lifecycle import schedule_delete_later
from termana.app.window_contracts import (
    CONFIRMATION_MESSAGE_TIMEOUT_MS,
    LOGGER,
    PLACEHOLDER_MESSAGE_TIMEOUT_MS,
    _bounded_session_name,
    _ConnectionOpenState,
    _format_network_endpoint,
    _is_valid_ssh_credential_generation,
    _lookup_ssh_login_options,
    _SshConnectOrigin,
    _SshLoginLookupResult,
    _SshLoginLookupStatus,
    _SshLoginResolution,
    _SshLoginResolutionOperation,
    _SshLoginResolutionRequest,
    _SshPreferenceOperationContext,
    _SshPreferenceRequest,
)
from termana.models.connection import ConnectionProfile
from termana.models.ssh_credential import SshCredentialRef, SshEndpoint
from termana.models.ssh_identity import SshAuthenticationMode
from termana.ssh.dialogs.ssh_login_dialog import (
    SavedSshLoginOption,
    SshLoginDialog,
    SshLoginSelection,
)
from termana.ssh.ssh_target import SshTarget, format_ssh_target
from termana.ui.i18n import tr


def _resolve_ssh_login(
    self: MainWindow,
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
        catalog_profile_snapshot = saved_profile
        profile = replace(profile, username=saved_profile.username)
    manual_profile = self._profile_without_ssh_username(profile, is_saved_profile=is_saved_profile)
    connection_profile = manual_profile if is_saved_profile else profile
    repository = self._credential_repository
    launch_guard = (
        (lambda: self.terminal_workspace.terminal_reconnect_pending(reconnect_session_id))
        if reconnect_session_id is not None
        else None
    )
    if self._embedded_ssh_known_hosts is None and (
        repository is None or profile.ssh_identity.authentication is SshAuthenticationMode.KEY_ONLY
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
            origin=origin, reconnect_session_id=reconnect_session_id, succeeded=bool(outcome)
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
            origin=origin, reconnect_session_id=reconnect_session_id, succeeded=bool(outcome)
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
    task = BackgroundOperation(request.request_id, lambda: _lookup_ssh_login_options(request))
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
            request, notice=tr("Saved logins are unavailable; password entry remains manual.")
        )
        operation.completed = True
        operation.outcome = outcome
        self._finish_ssh_connect_origin(request, succeeded=bool(outcome))
        self._resume_close_after_ssh_repository_operations()
        return outcome
    return operation.outcome if operation.completed else _ConnectionOpenState.PENDING


def _active_ssh_login_resolution(self: MainWindow) -> _SshLoginResolutionOperation | None:
    """Return the exact active lookup, if it has not already been consumed."""
    operation_id = self._active_ssh_login_resolution_id
    if operation_id is None:
        return None
    return self._ssh_login_resolution_operations.get(operation_id)


def _same_ssh_connection_profile(
    first: ConnectionProfile | None, second: ConnectionProfile
) -> bool:
    """Ignore only catalog decoration when validating a connection intent."""
    return first is not None and replace(first, is_favorite=second.is_favorite) == second


def _same_ssh_login_request(
    first: _SshLoginResolutionRequest, second: _SshLoginResolutionRequest
) -> bool:
    """Deduplicate only the same immutable connection intent."""
    from termana.app.main_window import MainWindow

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
        and (first.repository is second.repository)
        and (first.recovery_attempted == second.recovery_attempted)
        and (first.origin is second.origin)
        and (first.reconnect_session_id == second.reconnect_session_id)
        and (first.pane_id == second.pane_id)
        and (first.embedded_known_hosts == second.embedded_known_hosts)
        and (first.embedded_agent_path == second.embedded_agent_path)
    )


def _abandon_ssh_login_resolution(
    self: MainWindow, operation: _SshLoginResolutionOperation
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
    self: MainWindow, request: _SshLoginResolutionRequest | None
) -> None:
    """Expose a compact, non-modal pending state without freezing the UI."""
    pending = request is not None
    self._ssh_login_progress.setVisible(pending)
    if pending:
        self.statusBar().showMessage(tr("Checking saved SSH logins…"))


def _handle_ssh_login_lookup_succeeded(
    self: MainWindow, operation_id: object, result: object
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
            request, notice=tr("Saved logins are unavailable; password entry remains manual.")
        )
    else:
        outcome = self._apply_ssh_login_lookup_result(request, result)
    operation.completed = True
    operation.outcome = outcome
    self._finish_ssh_connect_origin(request, succeeded=bool(outcome))


def _handle_ssh_login_lookup_failed(self: MainWindow, operation_id: object) -> None:
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
        request, notice=tr("Saved logins are unavailable; password entry remains manual.")
    )
    operation.completed = True
    operation.outcome = outcome
    self._finish_ssh_connect_origin(request, succeeded=bool(outcome))


def _take_ssh_login_resolution_operation(
    self: MainWindow, operation_id: object
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


def _ssh_login_request_is_current(self: MainWindow, request: _SshLoginResolutionRequest) -> bool:
    """Reject late results whose profile, repository, or owning tab changed."""
    pane_id = self._ssh_login_request_pane_id(request)
    if (
        self._close_waiting_for_terminals
        or self._credential_repository is not request.repository
        or self._embedded_ssh_known_hosts != request.embedded_known_hosts
        or (self._embedded_ssh_agent_path != request.embedded_agent_path)
        or (pane_id is None)
        or (not self.terminal_workspace.pane_available(pane_id))
    ):
        return False
    if request.is_saved_profile:
        catalog_snapshot = request.catalog_profile_snapshot
        if (
            catalog_snapshot is None
            or catalog_snapshot.profile_id != request.profile.profile_id
            or (
                not self._same_ssh_connection_profile(
                    self._connection_catalog.profile_by_id(request.profile.profile_id),
                    catalog_snapshot,
                )
            )
        ):
            return False
    reconnect_session_id = request.reconnect_session_id
    return reconnect_session_id is None or self.terminal_workspace.terminal_reconnect_pending(
        reconnect_session_id
    )


def _ssh_login_request_pane_id(
    self: MainWindow, request: _SshLoginResolutionRequest
) -> UUID | None:
    """Reconnect follows its existing page; an unopened request keeps its pane."""
    if request.reconnect_session_id is not None:
        return self.terminal_workspace.pane_id_for_session(request.reconnect_session_id)
    return request.pane_id


def _valid_ssh_login_lookup_result(request: _SshLoginResolutionRequest, result: object) -> bool:
    """Validate every worker-owned metadata field before constructing UI."""
    if (
        not isinstance(result, _SshLoginLookupResult)
        or result.endpoint != request.endpoint
        or (not isinstance(result.status, _SshLoginLookupStatus))
        or (
            result.explicit_credential_id is not None
            and (not isinstance(result.explicit_credential_id, UUID))
        )
        or (
            result.explicit_credential_generation is not None
            and (not _is_valid_ssh_credential_generation(result.explicit_credential_generation))
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
            and (result.explicit_credential_generation is None)
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
    self: MainWindow, request: _SshLoginResolutionRequest, result: _SshLoginLookupResult
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
            request,
            _SshLoginResolution(
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
            request, _SshLoginResolution(profile=connection_profile)
        )
    dialog = SshLoginDialog(
        endpoint=_format_network_endpoint(request.profile.host, request.profile.port),
        options=tuple(

                SavedSshLoginOption(
                    credential_id=credential.credential_id, username=credential.username
                )
                for credential in result.credentials

        ),
        parent=self,
    )
    self._ssh_login_dialogs[request.request_id] = dialog
    removal = None
    if getattr(dialog, "remove_requested", None) is not None:
        from termana.ssh.ssh_login_removal import SshLoginRemovalController

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
            lambda message: self.statusBar().showMessage(message, CONFIRMATION_MESSAGE_TIMEOUT_MS)
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
            tr("The selected saved SSH login is no longer valid."), PLACEHOLDER_MESSAGE_TIMEOUT_MS
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
            request.profile, selection.username, is_saved_profile=request.is_saved_profile
        )
        if selection.username is not None
        else self._profile_without_ssh_username(
            request.profile, is_saved_profile=request.is_saved_profile
        )
    )
    preference_request = _SshPreferenceRequest(
        endpoint=request.endpoint,
        credential_id=selection.credential_id if selection.remember_for_profile else None,
        profile_id=request.profile.profile_id if request.is_saved_profile else None,
    )
    return self._open_resolved_ssh_lookup(
        request,
        _SshLoginResolution(
            profile=resolved_profile,
            credential_id=selection.credential_id,
            credential_generation=selected_credential.generation
            if selected_credential is not None
            else None,
            preference_request=preference_request,
        ),
    )


def _open_manual_ssh_login_after_lookup_failure(
    self: MainWindow,
    request: _SshLoginResolutionRequest,
    *,
    notice: str,
    preparation_completed: bool = False,
) -> _ConnectionOpenState:
    """Keep Connect usable when a repository lookup cannot complete."""
    if request.embedded_known_hosts is not None and (not preparation_completed):
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
        request, _SshLoginResolution(profile=profile, notice=notice)
    )


def _open_resolved_ssh_lookup(
    self: MainWindow, request: _SshLoginResolutionRequest, resolution: _SshLoginResolution
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
    self: MainWindow, request: _SshLoginResolutionRequest, *, succeeded: bool
) -> None:
    """Resolve reconnect only after actual flow acceptance."""
    self._finish_immediate_ssh_connect_origin(
        origin=request.origin,
        reconnect_session_id=request.reconnect_session_id,
        succeeded=succeeded,
    )


def _finish_immediate_ssh_connect_origin(
    self: MainWindow,
    *,
    origin: _SshConnectOrigin,
    reconnect_session_id: UUID | None,
    succeeded: bool,
) -> None:
    """Finish one initiating UI transaction after a non-pending launch result."""
    if origin is _SshConnectOrigin.RECONNECT:
        session_id = reconnect_session_id
        if session_id is not None:
            self.terminal_workspace.finish_terminal_reconnect(session_id, succeeded=succeeded)


def _profile_with_ssh_username(
    profile: ConnectionProfile, username: str, *, is_saved_profile: bool
) -> ConnectionProfile:
    """Apply one selected username and keep unsaved session tab titles precise."""
    if is_saved_profile:
        return replace(profile, username=username)
    target = SshTarget(host=profile.host, port=profile.port, username=username)
    return replace(
        profile, name=_bounded_session_name(format_ssh_target(target)), username=username
    )


def _profile_without_ssh_username(
    profile: ConnectionProfile, *, is_saved_profile: bool
) -> ConnectionProfile:
    """Clear an implicit login while retaining a stable saved-profile title."""
    if is_saved_profile:
        return replace(profile, username=None)
    target = SshTarget(host=profile.host, port=profile.port)
    return replace(profile, name=_bounded_session_name(format_ssh_target(target)), username=None)


def _commit_ssh_preference(self: MainWindow, request: _SshPreferenceRequest | None) -> bool:
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
    context = _SshPreferenceOperationContext(request=request, repository=repository)
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


def _handle_ssh_preference_succeeded(
    self: MainWindow, operation_id: object, result: object
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


def _handle_ssh_preference_failed(self: MainWindow, operation_id: object) -> None:
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
    self: MainWindow, operation_id: object
) -> tuple[BackgroundOperation, _SshPreferenceOperationContext] | None:
    """Consume exactly one preference completion identified by UUID."""
    if not isinstance(operation_id, UUID):
        LOGGER.warning("An SSH login preference write returned an invalid identity")
        return None
    return self._ssh_preference_operations.pop(operation_id, None)


def _show_ssh_preference_error(self: MainWindow) -> None:
    """Keep a successful terminal open after a retryable preference failure."""
    if not self._close_waiting_for_terminals:
        self.statusBar().showMessage(
            tr("Connected, but the login choice could not be remembered."),
            PLACEHOLDER_MESSAGE_TIMEOUT_MS,
        )
