"""Version-gated password proof for the primary embedded SSH engine.

AsyncSSH's public ``auth_completed`` callback doesn't identify the successful
method or expose partial success. Keep the small private integration here, not
in UI code. Its packet observers are per connection, never global monkeypatches.
An unreviewed library version fails closed before a password is requested.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Final
from uuid import UUID

import asyncssh
from asyncssh.auth import MSG_USERAUTH_PASSWD_CHANGEREQ, _ClientPasswordAuth
from asyncssh.packet import SSHPacket

from termana.credentials.embedded_host_trust import EmbeddedHostTrust
from termana.credentials.secret_store import MAX_SSH_PASSWORD_CHARACTERS
from termana.models.ssh_credential import SshEndpoint, validate_ssh_username

REVIEWED_ASYNCSSH_VERSION: Final = "2.24.1"
MAX_EMBEDDED_PASSWORD_ATTEMPTS: Final = 3
MAX_EMBEDDED_PASSWORD_BYTES: Final = MAX_SSH_PASSWORD_CHARACTERS * 4
EMBEDDED_PASSWORD_PROOF_TIMEOUT_SECONDS: Final = 30

type EmbeddedPasswordProvider = Callable[[], Awaitable[str | None]]
type EmbeddedPublicKeyProvider = Callable[
    [], Awaitable[asyncssh.SSHKey | asyncssh.SSHKeyPair | None]
]
type EmbeddedDisconnectCallback = Callable[[], None]


class EmbeddedAuthContractError(RuntimeError):
    """The reviewed library protocol integration is unavailable."""


def require_embedded_auth_contract() -> None:
    """Refuse to make authentication claims against unreviewed internals."""
    if asyncssh.__version__ != REVIEWED_ASYNCSSH_VERSION:
        raise EmbeddedAuthContractError("The embedded SSH library needs a compatibility review.")
    handlers = getattr(asyncssh.SSHClientConnection, "_packet_handlers", None)
    if not isinstance(handlers, dict) or any(
        not callable(getattr(asyncssh.SSHClientConnection, method, None))
        or handlers.get(packet) is not getattr(asyncssh.SSHClientConnection, method, None)
        for packet, method in (
            (asyncssh.MSG_USERAUTH_FAILURE, "_process_userauth_failure"),
            (asyncssh.MSG_USERAUTH_SUCCESS, "_process_userauth_success"),
        )
    ):
        raise EmbeddedAuthContractError("The embedded SSH authentication contract changed.")
    password_handlers = getattr(_ClientPasswordAuth, "_packet_handlers", None)
    password_change = getattr(_ClientPasswordAuth, "_process_password_change", None)
    if (
        not isinstance(password_handlers, dict)
        or not callable(password_change)
        or password_handlers.get(MSG_USERAUTH_PASSWD_CHANGEREQ) is not password_change
    ):
        raise EmbeddedAuthContractError("The embedded SSH password-change contract changed.")


@dataclass(frozen=True, slots=True, eq=False)
class ProvenSshPassword:
    """One confidential, single-consumption result, never a general Qt signal."""

    session_id: UUID
    endpoint: SshEndpoint
    username: str
    password: str = field(repr=False)


class EmbeddedPasswordClient(asyncssh.SSHClient):
    """Bind accepted passwords to actual SSH packets from one verified connection."""

    def __init__(
        self,
        session_id: UUID,
        endpoint: SshEndpoint,
        username: str,
        password_provider: EmbeddedPasswordProvider,
        *,
        public_key_provider: EmbeddedPublicKeyProvider | None = None,
        disconnect_callback: EmbeddedDisconnectCallback | None = None,
        host_trust: EmbeddedHostTrust | None = None,
        connection_made_callback: Callable[[], None] | None = None,
    ) -> None:
        require_embedded_auth_contract()
        if not isinstance(session_id, UUID) or type(endpoint) is not SshEndpoint:
            raise ValueError("Embedded SSH requires an exact session and endpoint.")
        validate_ssh_username(username)
        if not callable(password_provider):
            raise TypeError("Embedded SSH requires a password provider.")
        if public_key_provider is not None and not callable(public_key_provider):
            raise TypeError("Embedded SSH requires a callable key provider.")
        if disconnect_callback is not None and not callable(disconnect_callback):
            raise TypeError("Embedded SSH requires a callable disconnect observer.")
        if host_trust is not None and type(host_trust) is not EmbeddedHostTrust:
            raise TypeError("Embedded SSH requires an owned host-trust policy.")
        if connection_made_callback is not None and not callable(connection_made_callback):
            raise TypeError("Embedded SSH requires a callable connection observer.")
        self._session_id = session_id
        self._endpoint = endpoint
        self._username = username
        self._provider: EmbeddedPasswordProvider | None = password_provider
        self._public_key_provider: EmbeddedPublicKeyProvider | None = public_key_provider
        self._disconnect_callback = disconnect_callback
        self._host_trust = host_trust
        self._connection_made_callback = connection_made_callback
        self._connection: asyncssh.SSHClientConnection | None = None
        self._candidate: str | None = None
        self._proof: ProvenSshPassword | None = None
        self._proof_deadline = 0.0
        self._proof_timeout_handle: asyncio.TimerHandle | None = None
        self._attempts = 0
        self._partial_success = False
        self._password_change = False
        self._authenticated = False
        self._closed = False

    @property
    def password_proof_deadline(self) -> float:
        """Expose non-secret expiry metadata without extending packet proof."""
        return self._proof_deadline

    @property
    def authentication_live(self) -> bool:
        return self._authenticated and not self._closed

    def connection_made(self, conn: asyncssh.SSHClientConnection) -> None:
        """Observe only this connection before its handshake/authentication starts."""
        require_embedded_auth_contract()
        if (
            type(conn) is not asyncssh.SSHClientConnection
            or self._connection is not None
            or self._closed
        ):
            raise EmbeddedAuthContractError("Unexpected embedded SSH connection ownership.")
        self._connection = conn
        if self._connection_made_callback is not None:
            self._connection_made_callback()
        handlers = conn._packet_handlers.copy()
        failure = handlers[asyncssh.MSG_USERAUTH_FAILURE]
        success = handlers[asyncssh.MSG_USERAUTH_SUCCESS]

        def observe_failure(connection, packet_type, packet_id, packet):
            # Decode a copy: the library must still validate the original packet.
            observed = SSHPacket(packet.get_remaining_payload())
            observed.get_namelist()
            partial = observed.get_boolean()
            observed.check_end()
            self._partial_success |= partial
            self._candidate = None
            self.discard_proven_password()
            return failure(connection, packet_type, packet_id, packet)

        def observe_success(connection, packet_type, packet_id, packet):
            observed = SSHPacket(packet.get_remaining_payload())
            observed.check_end()
            auth = connection._auth
            eligible = (
                not self._closed
                and not self._partial_success
                and not self._password_change
                and not connection._auth_was_trivial
                and getattr(auth, "_method", None) == b"password"
                and self._candidate is not None
            )
            result = success(connection, packet_type, packet_id, packet)
            if eligible and self._authenticated and not self._closed:
                self._proof = ProvenSshPassword(
                    self._session_id, self._endpoint, self._username, self._candidate
                )
                self._proof_deadline = monotonic() + EMBEDDED_PASSWORD_PROOF_TIMEOUT_SECONDS
                self._proof_timeout_handle = asyncio.get_running_loop().call_later(
                    EMBEDDED_PASSWORD_PROOF_TIMEOUT_SECONDS, self.discard_proven_password
                )
            self._candidate = None
            self._provider = None
            return result

        handlers[asyncssh.MSG_USERAUTH_FAILURE] = observe_failure
        handlers[asyncssh.MSG_USERAUTH_SUCCESS] = observe_success
        conn._packet_handlers = handlers

    async def password_auth_requested(self) -> str | None:
        """Acquire bounded in-memory input; late cancelled results are discarded."""
        self._candidate = None
        if self._closed or self._attempts >= MAX_EMBEDDED_PASSWORD_ATTEMPTS:
            return None
        provider = self._provider
        if provider is None:
            return None
        await self.prepare_host_trust()
        if self._closed:
            return None
        self._observe_password_change_packets()
        self._attempts += 1
        try:
            password = await provider()
        except Exception:
            raise ValueError("The embedded SSH password provider failed.") from None
        if self._closed:
            return None
        if password is None:
            return None
        if (
            type(password) is not str
            or len(password) > MAX_SSH_PASSWORD_CHARACTERS
            or "\0" in password
        ):
            raise ValueError("The embedded SSH password is invalid.")
        try:
            encoded_length = len(password.encode("utf-8"))
        except UnicodeError:
            raise ValueError("The embedded SSH password is invalid.") from None
        if encoded_length > MAX_EMBEDDED_PASSWORD_BYTES:
            raise ValueError("The embedded SSH password is too long.")
        self._candidate = password
        return password

    def _observe_password_change_packets(self) -> None:
        """Retire proof at packet time, before a coalesced SUCCESS can cancel callbacks."""
        if self._connection is None:
            return  # Standalone provider checks cannot establish packet proof.
        auth = self._connection._auth
        if type(auth) is not _ClientPasswordAuth or auth._method != b"password":
            raise EmbeddedAuthContractError("Unexpected embedded SSH password-auth ownership.")
        handlers = auth._packet_handlers.copy()
        original = handlers[MSG_USERAUTH_PASSWD_CHANGEREQ]

        def observe_change(owner, packet_type, packet_id, packet):
            observed = SSHPacket(packet.get_remaining_payload())
            observed.get_string()
            observed.get_string()
            observed.check_end()
            self._password_change = True
            self._candidate = None
            self.discard_proven_password()
            return original(owner, packet_type, packet_id, packet)

        handlers[MSG_USERAUTH_PASSWD_CHANGEREQ] = observe_change
        auth._packet_handlers = handlers

    async def public_key_auth_requested(self) -> asyncssh.SSHKey | asyncssh.SSHKeyPair | None:
        """Key success is deliberately never interpreted as password proof."""
        self._candidate = None
        self.discard_proven_password()
        provider = self._public_key_provider
        if self._closed or provider is None:
            return None
        await self.prepare_host_trust()
        if self._closed:
            return None
        key = await provider()
        return None if self._closed else key

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        """Only exact synchronous user consent can supplement supplied trust."""
        if self._closed or self._host_trust is None:
            return False
        try:
            return self._host_trust.validate(host, addr, port, key) is True
        except Exception:
            return False  # No provider/file error can authorize or disclose trust.

    async def prepare_host_trust(self) -> None:
        """Library has completed key exchange before asking for credentials."""
        if self._host_trust is not None:
            if self._closed or self._connection is None:
                raise EmbeddedAuthContractError("The embedded SSH owner ended.")
            await self._host_trust.prepare_credentials(self._connection)

    def password_change_requested(self, _prompt: str, _lang: str):
        """Do not offer to save an expired password or implement credential changes."""
        self._password_change = True
        self._candidate = None
        self.discard_proven_password()
        return NotImplemented

    def auth_completed(self) -> None:
        """Completion alone is deliberately not password proof."""
        self._authenticated = True
        self._public_key_provider = None

    def take_proven_password(
        self, connection: asyncssh.SSHClientConnection
    ) -> ProvenSshPassword | None:
        """Consume only a live, exact connection's accepted password, once."""
        if self._closed or connection is not self._connection:
            return None
        proof, self._proof = self._proof, None
        self._cancel_proof_timeout()
        if monotonic() > self._proof_deadline:
            return None
        return proof

    def discard_proven_password(self) -> None:
        self._proof = None
        self._cancel_proof_timeout()

    def _cancel_proof_timeout(self) -> None:
        if self._proof_timeout_handle is not None:
            self._proof_timeout_handle.cancel()
            self._proof_timeout_handle = None

    def connection_lost(self, _exc: Exception | None) -> None:
        self.close()

    def close(self) -> None:
        """Tombstone this owner without retaining a password or pending provider."""
        if self._closed:
            return
        self._closed = True
        self._candidate = None
        self.discard_proven_password()
        self._provider = None
        self._public_key_provider = None
        self._connection = None
        self._host_trust = None
        self._connection_made_callback = None
        callback, self._disconnect_callback = self._disconnect_callback, None
        if callback is not None:
            callback()
