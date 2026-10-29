"""UI-thread routing of auth prompts, without window, vault or transport policy."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from termana.credentials.ssh_auth import SshSecretRequest


class SshSecretSession(Protocol):
    """The only authentication operations available to the prompt router."""

    def submit_secret(self, request_id: UUID, secret: str) -> bool: ...

    def cancel_secret(self, request_id: UUID) -> bool: ...


class SshPromptWorkspace(Protocol):
    """Exact-session prompt operations, never an active-tab lookup or PTY write."""

    def begin_ssh_secret_prompt(
        self,
        session_id: UUID,
        request_id: UUID,
        prompt: str,
        *,
        is_host_password: bool = True,
        is_host_confirmation: bool = False,
    ) -> bool: ...

    def finish_ssh_secret_prompt(self, session_id: UUID, request_id: UUID) -> bool: ...

    def close_terminal_session(self, session_id: UUID) -> bool: ...


@dataclass(frozen=True, slots=True)
class _PromptOwner:
    authentication_id: UUID
    terminal_session_id: UUID


class SshSecretPromptController:
    """Bind opaque request IDs to their original authentication and terminal.

    Callbacks resolve current owners at dispatch time. A response is passed only
    to that authentication, never stored here, logged or written to a terminal.
    The coordinator retains auth lifetime, proof, trust and credential decisions.
    """

    def __init__(
        self,
        workspace: SshPromptWorkspace,
        *,
        authentication_for: Callable[[UUID], SshSecretSession | None],
        terminal_for: Callable[[UUID], UUID | None],
        is_closing: Callable[[], bool],
    ) -> None:
        self._workspace = workspace
        self._authentication_for = authentication_for
        self._terminal_for = terminal_for
        self._is_closing = is_closing
        self._owners: dict[UUID, _PromptOwner] = {}

    @property
    def pending_request_count(self) -> int:
        return len(self._owners)

    def request(self, authentication_id: UUID, request: object) -> None:
        """Present one request only in its original, still-owned terminal."""
        authentication = self._authentication_for(authentication_id)
        terminal_session_id = self._terminal_for(authentication_id)
        if (
            self._is_closing()
            or authentication is None
            or terminal_session_id is None
            or not isinstance(request, SshSecretRequest)
            or request.request_id in self._owners
        ):
            if authentication is not None and isinstance(request, SshSecretRequest):
                authentication.cancel_secret(request.request_id)
            return
        self._owners[request.request_id] = _PromptOwner(authentication_id, terminal_session_id)
        if self._workspace.begin_ssh_secret_prompt(
            terminal_session_id,
            request.request_id,
            request.prompt,
            is_host_password=request.is_host_password,
            is_host_confirmation=request.is_host_confirmation,
        ):
            return
        self._owners.pop(request.request_id, None)
        authentication.cancel_secret(request.request_id)

    def finish(self, authentication_id: UUID, request: object) -> None:
        """Clear only the editor belonging to this completed auth request."""
        if not isinstance(request, SshSecretRequest):
            return
        owner = self._owners.get(request.request_id)
        if owner is None or owner.authentication_id != authentication_id:
            return
        self._owners.pop(request.request_id, None)
        self._workspace.finish_ssh_secret_prompt(owner.terminal_session_id, request.request_id)

    def submit(self, terminal_session_id: object, request_id: object, secret: str) -> None:
        """Pass one response to its exact auth owner, never a selected tab."""
        try:
            if not isinstance(terminal_session_id, UUID) or not isinstance(request_id, UUID):
                return
            owner = self._owners.get(request_id)
            if owner is None or owner.terminal_session_id != terminal_session_id:
                self._workspace.finish_ssh_secret_prompt(terminal_session_id, request_id)
                return
            authentication = self._authentication_for(owner.authentication_id)
            if authentication is not None and authentication.submit_secret(request_id, secret):
                return
            self._retire_failed_response(terminal_session_id, request_id)
        finally:
            secret = ""

    def cancel(self, terminal_session_id: object, request_id: object) -> None:
        """Cancel only the request displayed in the exact terminal tab."""
        if not isinstance(terminal_session_id, UUID) or not isinstance(request_id, UUID):
            return
        owner = self._owners.get(request_id)
        if owner is None or owner.terminal_session_id != terminal_session_id:
            self._workspace.finish_ssh_secret_prompt(terminal_session_id, request_id)
            return
        authentication = self._authentication_for(owner.authentication_id)
        if authentication is not None and authentication.cancel_secret(request_id):
            return
        self._retire_failed_response(terminal_session_id, request_id)

    def _retire_failed_response(self, terminal_session_id: UUID, request_id: UUID) -> None:
        self._owners.pop(request_id, None)
        self._workspace.finish_ssh_secret_prompt(terminal_session_id, request_id)
        self._workspace.close_terminal_session(terminal_session_id)

    def clear_authentication(self, authentication_id: UUID) -> None:
        """Release only this auth flow, even when finishing a prompt reenters UI."""
        owned = [
            (request_id, owner.terminal_session_id)
            for request_id, owner in self._owners.items()
            if owner.authentication_id == authentication_id
        ]
        for request_id, terminal_session_id in owned:
            self._owners.pop(request_id, None)
            self._workspace.finish_ssh_secret_prompt(terminal_session_id, request_id)
