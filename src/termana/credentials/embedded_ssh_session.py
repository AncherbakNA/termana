"""GUI-owned inline authentication and consent for the embedded SSH backend."""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal, Slot
from PySide6.QtWidgets import QDialog, QWidget

from termana.credentials.background_operation import ConfidentialBackgroundOperation
from termana.credentials.embedded_gateway_view import EmbeddedGatewayView
from termana.credentials.repository import SshCredentialPasswordSnapshot, SshCredentialRepository
from termana.credentials.ssh_auth import (
    SshCredentialMutationCoordinator,
    SshCredentialMutationOutcome,
    SshSecretRequest,
)
from termana.dialogs.ssh_auth_dialogs import (
    confirm_save_ssh_password,
    confirm_saved_gateway_password,
)
from termana.models.ssh_credential import validate_ssh_credential_generation
from termana.models.ssh_jump import SshJumpMode
from termana.terminal.embedded_ssh_backend import EmbeddedSshBackend
from termana.terminal.legacy_ssh_backend import LegacySshBackend


class EmbeddedSshAuthenticationSession(QObject):
    """Reuse terminal-owned prompts and the application-scoped keychain writer."""

    secret_requested = Signal(object)
    secret_request_finished = Signal(object)
    cancel_requested = Signal()
    status_message = Signal(str)

    def __init__(
        self,
        backend: EmbeddedSshBackend | EmbeddedGatewayView | LegacySshBackend,
        *,
        repository: SshCredentialRepository | None,
        credential_id: UUID | None,
        credential_generation: int | None,
        credential_mutation_coordinator: SshCredentialMutationCoordinator,
        owner_live: Callable[[], bool],
        parent: QObject | None = None,
        save_confirmation_prompt=confirm_save_ssh_password,
        gateway_confirmation_prompt=confirm_saved_gateway_password,
        operation_starter=None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(backend, (EmbeddedSshBackend, EmbeddedGatewayView, LegacySshBackend)):
            raise TypeError("backend must be a reviewed SSH authentication backend")
        if repository is not None and not isinstance(repository, SshCredentialRepository):
            raise TypeError("repository must be an SshCredentialRepository or None")
        if not isinstance(credential_mutation_coordinator, SshCredentialMutationCoordinator):
            raise TypeError("credential mutations require the application-scoped coordinator")
        if not callable(owner_live) or not callable(save_confirmation_prompt):
            raise TypeError("owner and consent guards must be callable")
        if operation_starter is not None and not callable(operation_starter):
            raise TypeError("operation starter must be callable")
        if credential_id is not None and type(credential_id) is not UUID:
            raise TypeError("credential identity must be a UUID")
        if credential_generation is not None:
            validate_ssh_credential_generation(credential_generation)
        if (credential_id is None) != (credential_generation is None):
            raise ValueError("A saved login needs both its identity and generation")
        self.authentication_id = backend.authentication_id
        self._backend = backend
        self._repository = repository
        self._credential_id = credential_id
        self._generation = credential_generation
        self._coordinator = credential_mutation_coordinator
        self._owner_live = owner_live
        self._confirmation = save_confirmation_prompt
        self._gateway_confirmation = gateway_confirmation_prompt
        self._gateway_lookup_attempted = False
        self._gateway_lookup = None
        self._dialog_parent = parent
        while self._dialog_parent is not None and not isinstance(self._dialog_parent, QWidget):
            self._dialog_parent = self._dialog_parent.parent()
        self._starter = operation_starter or QThreadPool.globalInstance().start
        self._read: tuple[UUID, ConfidentialBackgroundOperation, SshSecretRequest] | None = None
        self._saved_attempted = False
        self._saved_used = False
        self._closed = False
        self._dialog: QDialog | None = None
        self._offered = False
        self._save_operation_id: UUID | None = None
        self._consent_expiry = QTimer(self)
        self._consent_expiry.setSingleShot(True)
        self._consent_expiry.timeout.connect(self._expire_consent)
        self._gateway_session = None
        if (
            isinstance(backend, EmbeddedSshBackend)
            and backend.plan.profile.ssh_jump.mode is SshJumpMode.JUMP
        ):
            view = EmbeddedGatewayView(backend, self)
            self._gateway_session = EmbeddedSshAuthenticationSession(
                view,
                repository=repository,
                credential_id=None,
                credential_generation=None,
                credential_mutation_coordinator=credential_mutation_coordinator,
                owner_live=owner_live,
                parent=self,
                save_confirmation_prompt=save_confirmation_prompt,
                gateway_confirmation_prompt=gateway_confirmation_prompt,
                operation_starter=self._starter,
            )
            self._gateway_session.secret_requested.connect(self.secret_requested)
            self._gateway_session.secret_request_finished.connect(self.secret_request_finished)
            self._gateway_session.status_message.connect(self.status_message)
            self._gateway_session.cancel_requested.connect(self.cancel_requested)
        backend.secret_requested.connect(self._request_secret)
        backend.secret_request_finished.connect(self._request_finished)
        backend.authenticated.connect(self._offer_save)
        credential_mutation_coordinator.operation_finished.connect(self._save_finished)

    def submit_secret(self, request_id: UUID, value: str) -> bool:
        if self._closed or not self._owner_live():
            return False
        request = self._backend.current_secret_request
        if self._gateway_session is not None and request is not None and request.is_gateway:
            return self._gateway_session.submit_secret(request_id, value)
        accepted = self._backend.submit_secret(request_id, value)
        if accepted:
            self._saved_used = False
        return accepted

    def cancel_secret(self, request_id: UUID) -> bool:
        request = self._backend.current_secret_request
        if self._closed or request is None:
            return False
        if request.request_id != request_id:
            return False
        self.close()
        self.cancel_requested.emit()
        return True

    @Slot(object)
    def _request_secret(self, request: SshSecretRequest) -> None:
        if request.is_gateway and self._gateway_session is not None:
            return
        if self._closed or not self._owner_live():
            self.close()
            return
        if (
            isinstance(self._backend, EmbeddedGatewayView)
            and request.is_host_password
            and self._repository is not None
            and not self._gateway_lookup_attempted
        ):
            self._gateway_lookup_attempted = True
            self._lookup_gateway(request)
            return
        if (
            not request.is_host_password
            or self._saved_attempted
            or self._credential_id is None
            or self._repository is None
        ):
            self.secret_requested.emit(request)
            return
        self._saved_attempted = True
        operation_id = uuid4()
        repository, credential_id = self._repository, self._credential_id
        endpoint = self._backend.plan.endpoint
        task = ConfidentialBackgroundOperation(
            operation_id,
            lambda: repository.password_for_with_generation(credential_id, endpoint),
        )
        self._read = (operation_id, task, request)
        task.signals.succeeded.connect(self._read_succeeded)
        task.signals.failed.connect(self._read_failed)
        try:
            self._starter(task)
        except Exception:
            task.abandon(operation_id)
            self._read_failed(operation_id)

    def _lookup_gateway(self, request):
        operation_id = uuid4()
        repository, endpoint = self._repository, self._backend.plan.endpoint
        # Metadata only: do not unlock the password before explicit gateway choice.
        task = ConfidentialBackgroundOperation(
            operation_id, lambda: repository.load_index().credentials_for(endpoint)
        )
        self._gateway_lookup = (operation_id, task, request)
        task.signals.succeeded.connect(self._gateway_lookup_finished)
        task.signals.failed.connect(self._gateway_lookup_failed)
        try:
            self._starter(task)
        except Exception:
            self._gateway_lookup_failed(operation_id)

    def _gateway_lookup_failed(self, operation_id):
        pending = self._gateway_lookup
        if pending is None or pending[0] != operation_id:
            return
        pending[1].abandon(operation_id)
        self._gateway_lookup = None
        if (
            not self._closed
            and self._owner_live()
            and self._backend.current_secret_request == pending[2]
        ):
            self.secret_requested.emit(pending[2])

    def _gateway_lookup_finished(self, operation_id):
        pending = self._gateway_lookup
        if pending is None or pending[0] != operation_id:
            return
        self._gateway_lookup = None
        _, task, request = pending
        references = task.take_result(operation_id)
        if (
            self._closed
            or not self._owner_live()
            or self._backend.current_secret_request != request
        ):
            return
        saved = next((item for item in references if item.username == request.username), None)
        if saved is None:
            self.secret_requested.emit(request)
            return

        def register(dialog):
            self._dialog = dialog
            return not self._closed and self._owner_live()

        arguments = dict(
            endpoint=request.endpoint, username=request.username, parent=self._dialog_parent
        )
        if self._gateway_confirmation is confirm_saved_gateway_password:
            arguments["dialog_created"] = register
        try:
            use_saved = self._gateway_confirmation(**arguments)
        except Exception:
            use_saved = False
        finally:
            self._dialog = None
        if (
            self._closed
            or not self._owner_live()
            or self._backend.current_secret_request != request
        ):
            return
        if use_saved is True:
            self._credential_id, self._generation = saved.credential_id, saved.generation
            self._request_secret(request)
        else:
            self.secret_requested.emit(request)

    @Slot(object)
    def _read_succeeded(self, operation_id: UUID) -> None:
        pending = self._read
        if pending is None or pending[0] != operation_id:
            return
        self._read = None
        _, task, request = pending
        snapshot = None
        try:
            snapshot = task.take_result(operation_id)
            if (
                self._closed
                or not self._owner_live()
                or self._backend.current_secret_request != request
            ):
                return
            if (
                isinstance(snapshot, SshCredentialPasswordSnapshot)
                and snapshot.generation == self._generation
                and self._backend.submit_secret(request.request_id, snapshot.password)
            ):
                self._saved_used = True
                return
            self.status_message.emit("The saved SSH login changed; enter the password manually.")
            self.secret_requested.emit(request)
        finally:
            snapshot = None

    @Slot(object)
    def _read_failed(self, operation_id: UUID) -> None:
        pending = self._read
        if pending is None or pending[0] != operation_id:
            return
        self._read = None
        pending[1].abandon(operation_id)
        if (
            not self._closed
            and self._owner_live()
            and self._backend.current_secret_request == pending[2]
        ):
            self.status_message.emit(
                "The saved SSH password could not be unlocked; enter it manually."
            )
            self.secret_requested.emit(pending[2])

    @Slot(object)
    def _request_finished(self, request: SshSecretRequest) -> None:
        if request.is_gateway and self._gateway_session is not None:
            return
        if (
            self._gateway_lookup is not None
            and self._gateway_lookup[2].request_id == request.request_id
        ):
            operation_id, task, _ = self._gateway_lookup
            task.abandon(operation_id)
            self._gateway_lookup = None
        if self._dialog is not None and not self._offered:
            self._dialog.reject()
        if self._read is not None and self._read[2].request_id == request.request_id:
            operation_id, task, _ = self._read
            task.abandon(operation_id)
            self._read = None
        self.secret_request_finished.emit(request)

    @Slot()
    def _offer_save(self) -> None:
        if self._offered:
            return
        self._offered = True
        proof = self._backend.take_proven_password()
        deadline = self._backend.password_proof_deadline
        try:
            plan = self._backend.plan
            if (
                proof is None
                or self._closed
                or not self._owner_live()
                or not self._backend.authentication_live
                or proof.session_id != self.authentication_id
                or proof.endpoint != plan.endpoint
                or proof.username != plan.profile.username
                or self._saved_used
                or self._repository is None
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._consent_expiry.start(max(1, int(remaining * 1000)))

            def register(dialog):
                if self._closed or not self._owner_live():
                    return False
                self._dialog = dialog
                return True

            arguments = dict(
                endpoint=f"{proof.endpoint.host}:{proof.endpoint.port}",
                username=proof.username,
                parent=self._dialog_parent,
            )
            if self._confirmation is confirm_save_ssh_password:
                arguments["dialog_created"] = register
            consent = self._confirmation(**arguments)
            if (
                consent is not True
                or self._closed
                or not self._owner_live()
                or not self._backend.authentication_live
                or time.monotonic() >= deadline
            ):
                if consent is not True and not self._closed and self._owner_live():
                    self.status_message.emit("SSH password was not saved.")
                return
            # An explicit, live consent authorizes a detached keychain transaction.
            self._save_operation_id = uuid4()
            self._coordinator.start_password_save(
                self._save_operation_id,
                self._repository,
                proof.endpoint,
                proof.username,
                proof.password,
            )
        except Exception:
            self._save_operation_id = None
            if not self._closed and self._owner_live():
                self.status_message.emit(
                    "Signed in, but the SSH password could not be saved securely."
                )
        finally:
            self._dialog = None
            self._consent_expiry.stop()
            proof = None

    @Slot()
    def _expire_consent(self) -> None:
        if self._dialog is not None:
            self._dialog.reject()

    @Slot(object, object)
    def _save_finished(self, operation_id: object, outcome: object) -> None:
        if self._save_operation_id is None or operation_id != self._save_operation_id:
            return
        self._save_operation_id = None
        if self._closed or not self._owner_live():
            return
        if outcome is SshCredentialMutationOutcome.SAVE_SUCCEEDED:
            self.status_message.emit(
                "SSH password was saved securely. Choose this login next time."
            )
        else:
            self.status_message.emit("Signed in, but the SSH password could not be saved securely.")

    def finish(self) -> bytes:
        self.close()
        return b""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._consent_expiry.stop()
        self._owner_live = lambda: False
        if self._gateway_session is not None:
            self._gateway_session.close()
        if self._gateway_lookup is not None:
            operation_id, task, _ = self._gateway_lookup
            task.abandon(operation_id)
            self._gateway_lookup = None
        if self._read is not None:
            operation_id, task, _ = self._read
            task.abandon(operation_id)
            self._read = None
        if self._dialog is not None:
            self._dialog.reject()
        self._backend.close()
