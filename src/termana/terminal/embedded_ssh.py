"""Primary SSH transport, with explicit compatibility boundaries.

No user SSH config, default keys/agent, proxy commands or secret subprocess IPC
is inherited. Selected keys and explicitly opted-in Unix agents are supported.
Unsupported profile settings are rejected before connecting, never downgraded.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID

import asyncssh

from termana.credentials.embedded_host_trust import EmbeddedHostKeyProvider, EmbeddedHostTrust
from termana.credentials.embedded_ssh_auth import (
    EmbeddedAuthContractError,
    EmbeddedDisconnectCallback,
    EmbeddedPasswordClient,
    EmbeddedPasswordProvider,
    ProvenSshPassword,
    require_embedded_auth_contract,
)
from termana.credentials.embedded_ssh_keys import EmbeddedIdentityError, EmbeddedKeySource
from termana.credentials.embedded_ssh_trust import EmbeddedTrustFileError, load_embedded_trust_file
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.models.ssh_credential import SshEndpoint
from termana.models.ssh_identity import SshAuthenticationMode
from termana.models.ssh_jump import SshJumpMode
from termana.models.ssh_negotiation import SshNegotiatedFacts
from termana.terminal.embedded_preflight import EmbeddedPreparationReason
from termana.terminal.ssh_negotiation import snapshot_ssh_negotiation

EMBEDDED_CONNECT_TIMEOUT_SECONDS: Final = 15
EMBEDDED_LOGIN_TIMEOUT_SECONDS: Final = 300
EMBEDDED_KEEPALIVE_INTERVAL_SECONDS: Final = 15
EMBEDDED_KEEPALIVE_COUNT_MAX: Final = 3
EMBEDDED_CLOSE_TIMEOUT_SECONDS: Final = 2
EMBEDDED_CHANNEL_WINDOW_BYTES: Final = 64 * 1024
EMBEDDED_CHANNEL_PACKET_BYTES: Final = 4 * 1024
MAX_EMBEDDED_TERMINAL_DIMENSION: Final = 32767
EMBEDDED_SSH_MAC_ALGORITHMS: Final[tuple[str, ...]] = (
    "hmac-sha2-256-etm@openssh.com",
    "hmac-sha2-512-etm@openssh.com",
    "hmac-sha2-256",
    "hmac-sha2-512",
)


class EmbeddedSshFailure(StrEnum):
    CONFIGURATION = "configuration"
    LIBRARY_CONTRACT = "library-contract"
    HOST_KEY = "host-key"
    AUTHENTICATION = "authentication"
    IDENTITY = "identity"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    TUNNEL = "tunnel"
    TERMINAL = "terminal"


class EmbeddedSshError(RuntimeError):
    """Only fixed diagnostic categories cross the transport boundary."""

    def __init__(
        self,
        failure: EmbeddedSshFailure,
        *,
        preparation_reason: EmbeddedPreparationReason = EmbeddedPreparationReason.SETTINGS,
    ) -> None:
        self.failure = failure
        self.preparation_reason = preparation_reason
        super().__init__(f"Embedded SSH failed ({failure.value}).")


@dataclass(frozen=True, slots=True)
class EmbeddedSshPlan:
    """Validated non-secret settings for one explicit direct connection."""

    profile: ConnectionProfile
    known_hosts: Path
    tunnels_confirmed: bool = False
    agent_path: Path | None = None

    def __post_init__(self) -> None:
        profile = self.profile
        if (
            type(profile) is not ConnectionProfile
            or profile.protocol is not ConnectionProtocol.SSH
            or profile.username is None
            or profile.ssh_algorithms.protocol == "1"
            or not isinstance(self.known_hosts, Path)
            or not self.known_hosts.is_absolute()
            or type(self.tunnels_confirmed) is not bool
        ):
            raise EmbeddedSshError(EmbeddedSshFailure.CONFIGURATION)
        # CONFIG is not equivalent to DIRECT: ignoring a user's ProxyCommand or
        # Host alias could authenticate to the wrong host. No implicit conversion.
        if profile.ssh_jump.mode is SshJumpMode.CONFIG:
            raise EmbeddedSshError(
                EmbeddedSshFailure.CONFIGURATION, preparation_reason=EmbeddedPreparationReason.ROUTE
            )
        if profile.ssh_jump.mode is SshJumpMode.JUMP and profile.ssh_jump.username is None:
            raise EmbeddedSshError(
                EmbeddedSshFailure.CONFIGURATION,
                preparation_reason=EmbeddedPreparationReason.GATEWAY_USERNAME,
            )
        identity = profile.ssh_identity
        mode = identity.authentication
        if (
            mode is SshAuthenticationMode.KEY_ONLY
            and identity.identity_file is None
            and self.agent_path is None
        ):
            raise EmbeddedSshError(
                EmbeddedSshFailure.CONFIGURATION,
                preparation_reason=EmbeddedPreparationReason.KEY_SOURCE,
            )
        if (
            mode is not SshAuthenticationMode.PASSWORD_ONLY
            and identity.identity_file is not None
            and not Path(identity.identity_file).is_absolute()
        ):
            raise EmbeddedSshError(
                EmbeddedSshFailure.CONFIGURATION,
                preparation_reason=EmbeddedPreparationReason.IDENTITY_PATH,
            )
        if self.agent_path is not None and (
            not isinstance(self.agent_path, Path)
            or not self.agent_path.is_absolute()
            or os.name != "posix"
            or identity.agent_disabled
            or identity.identities_only is True
            or mode is SshAuthenticationMode.PASSWORD_ONLY
        ):
            # Do not quietly ignore an agent opt-in or weaken IdentitiesOnly.
            # AsyncSSH's Win32 pipe reads are synchronous; cancellation is not
            # yet reviewed there, so system OpenSSH remains the Windows path.
            raise EmbeddedSshError(
                EmbeddedSshFailure.CONFIGURATION, preparation_reason=EmbeddedPreparationReason.AGENT
            )
        if not profile.ssh_tunnels.is_default and not self.tunnels_confirmed:
            raise EmbeddedSshError(
                EmbeddedSshFailure.CONFIGURATION,
                preparation_reason=EmbeddedPreparationReason.TUNNEL_CONSENT,
            )

    @property
    def endpoint(self) -> SshEndpoint:
        return SshEndpoint(self.profile.host, self.profile.port)

    def connection_options(self) -> asyncssh.SSHClientConnectionOptions:
        """Validate cipher support without connecting or reading user config/keys."""
        options = self.profile.ssh_options
        mode = self.profile.ssh_identity.authentication
        settings = dict(
            config=None,
            username=self.profile.username,
            known_hosts=str(self.known_hosts),
            # config=None alone still discovers ~/.ssh/ca-bundle.crt and
            # ~/.ssh/crt. Trust must come only from our bounded snapshot;
            # empty lists suppress defaults without disabling verification.
            x509_trusted_certs=[],
            x509_trusted_cert_paths=[],
            client_keys=None,
            agent_path=None,
            agent_forwarding=False,
            host_based_auth=False,
            public_key_auth=(
                mode is not SshAuthenticationMode.PASSWORD_ONLY
                and (
                    self.profile.ssh_identity.identity_file is not None
                    or self.agent_path is not None
                )
            ),
            kbdint_auth=False,
            password_auth=mode is not SshAuthenticationMode.KEY_ONLY,
            preferred_auth=(
                ["publickey"]
                if mode is SshAuthenticationMode.KEY_ONLY
                else ["password"]
                if mode is SshAuthenticationMode.PASSWORD_ONLY
                else ["publickey", "password"]
            ),
            disable_trivial_auth=True,
            gss_host=None,
            gss_kex=False,
            gss_auth=False,
            proxy_command=None,
            tunnel=None,
            # AsyncSSH 2.24.1's optional UMAC wrapper uses Nettle's pre-4.0
            # digest ABI. Pin this connection's reviewed HMAC policy instead
            # of probing native crypto or changing library/global defaults.
            # AEAD ciphers retain their own authentication without a MAC.
            mac_algs=EMBEDDED_SSH_MAC_ALGORITHMS,
            connect_timeout=(options.connect_timeout or EMBEDDED_CONNECT_TIMEOUT_SECONDS),
            login_timeout=EMBEDDED_LOGIN_TIMEOUT_SECONDS,
            keepalive_interval=(
                EMBEDDED_KEEPALIVE_INTERVAL_SECONDS
                if options.server_alive_interval is None
                else options.server_alive_interval
            ),
            keepalive_count_max=(options.server_alive_count_max or EMBEDDED_KEEPALIVE_COUNT_MAX),
        )
        if self.profile.ssh_cipher is not None:
            settings["encryption_algs"] = [self.profile.ssh_cipher]
        if self.profile.ssh_algorithms.kex:
            settings["kex_algs"] = list(self.profile.ssh_algorithms.kex)
        if self.profile.ssh_algorithms.host_keys:
            settings["server_host_key_algs"] = list(self.profile.ssh_algorithms.host_keys)
        try:
            prepared = asyncssh.SSHClientConnectionOptions(**settings)
        except (OSError, ValueError, TypeError):
            raise EmbeddedSshError(
                EmbeddedSshFailure.CONFIGURATION,
                preparation_reason=EmbeddedPreparationReason.OPTIONS,
            ) from None
        # AsyncSSH applies SASLprep/NFKC to usernames. A rewrite would send
        # credentials to a different account while labeling proof with the
        # original model identity. Stable Unicode remains supported.
        if prepared.username != self.profile.username:
            raise EmbeddedSshError(
                EmbeddedSshFailure.CONFIGURATION,
                preparation_reason=EmbeddedPreparationReason.USERNAME,
            )
        return prepared


def validate_embedded_terminal_size(columns: int, rows: int) -> None:
    if any(
        type(value) is not int or not 1 <= value <= MAX_EMBEDDED_TERMINAL_DIMENSION
        for value in (columns, rows)
    ):
        raise ValueError("Embedded SSH terminal dimensions are out of range.")


class EmbeddedSshConnection:
    """Own one authenticated connection and all of its confirmed local listeners."""

    def __init__(
        self,
        plan: EmbeddedSshPlan,
        connection: asyncssh.SSHClientConnection,
        client: EmbeddedPasswordClient,
    ) -> None:
        self._plan = plan
        self._connection = connection
        self._client = client
        self._listeners: list[asyncssh.SSHListener] = []
        self._terminal: asyncssh.SSHClientProcess | None = None
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._gateway: EmbeddedSshConnection | None = None

    @property
    def tunnel_transport(self):
        if not self.authentication_live:
            raise EmbeddedSshError(EmbeddedSshFailure.CONNECTION)
        return self._connection

    @property
    def password_proof_deadline(self) -> float:
        """Keep the original authentication-packet expiry across UI handoff."""
        return self._client.password_proof_deadline

    @property
    def authentication_live(self) -> bool:
        return not self._closed and self._client.authentication_live

    @property
    def negotiated_facts(self) -> SshNegotiatedFacts | None:
        """Worker-only destination snapshot; never gateway or pre-auth metadata."""
        if not self.authentication_live:
            return None
        facts = snapshot_ssh_negotiation(self._connection)
        return facts if self.authentication_live else None

    def take_proven_password(self) -> ProvenSshPassword | None:
        """No persistence here: a separate live-owner/consent gate is required."""
        if self._closed:
            return None
        return self._client.take_proven_password(self._connection)

    async def open_confirmed_tunnels(self) -> None:
        """Bind only loopback; partial launch failures release all owned listeners."""
        if self._closed or self._listeners:
            raise EmbeddedSshError(EmbeddedSshFailure.TUNNEL)
        try:
            for forward in self._plan.profile.ssh_tunnels.local_forwards:
                listener = await self._connection.forward_local_port(
                    "127.0.0.1",
                    forward.local_port,
                    forward.destination_host,
                    forward.destination_port,
                )
                if self._closed:
                    listener.close()
                    await listener.wait_closed()
                    raise EmbeddedSshError(EmbeddedSshFailure.TUNNEL)
                self._listeners.append(listener)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise EmbeddedSshError(EmbeddedSshFailure.TUNNEL) from None

    async def open_terminal(self, columns: int, rows: int) -> asyncssh.SSHClientProcess:
        """Request a real PTY; a peer rejecting PTY isn't silently treated as a shell."""
        validate_embedded_terminal_size(columns, rows)
        if self._closed or self._terminal is not None:
            raise EmbeddedSshError(EmbeddedSshFailure.TERMINAL)
        try:
            terminal = await self._connection.create_process(
                request_pty="force",
                term_type="xterm-256color",
                term_size=(columns, rows),
                encoding=None,
                env={},
                x11_forwarding=False,
                stderr=asyncssh.STDOUT,
                window=EMBEDDED_CHANNEL_WINDOW_BYTES,
                max_pktsize=EMBEDDED_CHANNEL_PACKET_BYTES,
            )
            if self._closed:
                terminal.close()
                raise EmbeddedSshError(EmbeddedSshFailure.TERMINAL)
            self._terminal = terminal
            return terminal
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise EmbeddedSshError(EmbeddedSshFailure.TERMINAL) from None

    async def open_sftp(self):
        """Open a file subsystem on this verified owner, never a second login."""
        if self._closed or not self.authentication_live:
            raise EmbeddedSshError(EmbeddedSshFailure.CONNECTION)
        return await self._connection.start_sftp_client()

    async def close(self) -> None:
        """Forget secrets immediately, then release channels/listeners within a bound."""
        if self._close_task is None:
            self._closed = True
            self._client.close()
            self._close_task = asyncio.create_task(self._release_resources())
        # Cancelling a waiter must not cancel cleanup of the owned transport.
        await asyncio.shield(self._close_task)

    async def _release_resources(self) -> None:
        for listener in self._listeners:
            listener.close()
        if self._terminal is not None:
            self._terminal.close()
        self._connection.close()
        try:
            async with asyncio.timeout(EMBEDDED_CLOSE_TIMEOUT_SECONDS):
                await asyncio.gather(*(listener.wait_closed() for listener in self._listeners))
                await self._connection.wait_closed()
        except TimeoutError:
            pass
        finally:
            self._connection.abort()
            self._listeners.clear()
            self._terminal = None
            if self._gateway is not None:
                await self._gateway.close()
                self._gateway = None


async def _connect_single_embedded_ssh(
    plan: EmbeddedSshPlan,
    session_id: UUID,
    password_provider: EmbeddedPasswordProvider,
    *,
    passphrase_provider: EmbeddedPasswordProvider | None = None,
    disconnect_callback: EmbeddedDisconnectCallback | None = None,
    host_key_provider: EmbeddedHostKeyProvider | None = None,
    tunnel=None,
) -> EmbeddedSshConnection:
    """Connect under one immutable policy, without fallback or exception disclosure."""
    if type(plan) is not EmbeddedSshPlan:
        raise EmbeddedSshError(EmbeddedSshFailure.CONFIGURATION)
    if plan.profile.ssh_jump.mode is SshJumpMode.JUMP and tunnel is None:
        raise EmbeddedSshError(EmbeddedSshFailure.CONFIGURATION)
    try:
        require_embedded_auth_contract()
        options = plan.connection_options()
        host_trust = None
        try:
            if host_key_provider is not None:
                host_trust = await asyncio.to_thread(
                    EmbeddedHostTrust, plan.known_hosts, plan.endpoint, host_key_provider
                )
                trust = host_trust.match_known_hosts
            else:
                trust = await asyncio.to_thread(load_embedded_trust_file, plan.known_hosts)
            options = asyncssh.SSHClientConnectionOptions(options, known_hosts=trust)
        except (EmbeddedTrustFileError, OSError, ValueError, UnicodeError):
            raise EmbeddedSshError(EmbeddedSshFailure.HOST_KEY) from None
        identity_file = plan.profile.ssh_identity.identity_file
        if plan.profile.ssh_identity.authentication is SshAuthenticationMode.PASSWORD_ONLY:
            identity_file = None
        keys = EmbeddedKeySource(
            Path(identity_file) if identity_file is not None else None,
            plan.agent_path,
            passphrase_provider,
        )
        tcp_connected = asyncio.Event() if host_trust is not None else None
        client = EmbeddedPasswordClient(
            session_id,
            plan.endpoint,
            plan.profile.username,
            password_provider,
            public_key_provider=keys.public_key_auth_requested,
            disconnect_callback=disconnect_callback,
            host_trust=host_trust,
            connection_made_callback=(tcp_connected.set if tcp_connected is not None else None),
        )
    except EmbeddedAuthContractError:
        raise EmbeddedSshError(EmbeddedSshFailure.LIBRARY_CONTRACT) from None
    owned = None
    handed_off = False
    try:
        try:
            connection = await _connect_with_trust_deadlines(
                plan, options, client, tcp_connected, tunnel=tunnel
            )
            owned = EmbeddedSshConnection(plan, connection, client)
            # Also cover servers accepting 'none': no credential callback ran.
            await client.prepare_host_trust()
            await owned.open_confirmed_tunnels()
        finally:
            # Keep ownership until signing resources finish closing. A cancelled
            # return must not orphan the authenticated transport or its listeners.
            keys.close()
            await keys.wait_closed()
        handed_off = True
        return owned
    except asyncio.CancelledError:
        raise
    except EmbeddedSshError as error:
        failure = error.failure
    except asyncssh.HostKeyNotVerifiable:
        failure = EmbeddedSshFailure.HOST_KEY
    except EmbeddedTrustFileError:
        failure = EmbeddedSshFailure.HOST_KEY
    except asyncssh.PermissionDenied:
        failure = EmbeddedSshFailure.AUTHENTICATION
    except EmbeddedIdentityError:
        failure = EmbeddedSshFailure.IDENTITY
    except TimeoutError:
        failure = EmbeddedSshFailure.TIMEOUT
    except Exception:
        failure = EmbeddedSshFailure.CONNECTION
    finally:
        if not handed_off:
            client.close()
            if owned is not None:
                await owned.close()
    # Leave the original exception context; fixed messages must not disclose
    # server text, provider failures, local paths, or confidential credentials.
    raise EmbeddedSshError(failure)


async def _connect_with_trust_deadlines(plan, options, client, connected, *, tunnel=None):
    """Bound TCP separately from user interaction, using public client callbacks."""
    if connected is None:
        return await asyncssh.connect(
            plan.profile.host,
            plan.profile.port,
            config=None,
            options=options,
            client_factory=lambda: client,
            tunnel=tunnel,
        )
    timeout = options.connect_timeout
    # AsyncSSH connect_timeout covers TCP + KEX + all user authentication. The
    # reviewed login_timeout still bounds the latter; our gate has its own clock.
    interactive_options = asyncssh.SSHClientConnectionOptions(
        options,
        connect_timeout=None,
        # Otherwise known ECDSA keys restrict negotiation to ECDSA, preventing
        # review of an ED25519 replacement. Use the library's NORMAL defaults,
        # not all supported/legacy algorithms. Exact key trust is still required.
        server_host_key_algs=(list(plan.profile.ssh_algorithms.host_keys)
                              if plan.profile.ssh_algorithms.host_keys else "default"),
    )
    task = asyncio.ensure_future(
        asyncssh.connect(
            plan.profile.host,
            plan.profile.port,
            config=None,
            options=interactive_options,
            client_factory=lambda: client,
            tunnel=tunnel,
        )
    )
    waiting = asyncio.create_task(connected.wait())
    handed_off = False
    try:
        await asyncio.wait((task, waiting), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not task.done() and not connected.is_set():
            raise TimeoutError
        connection = await task
        handed_off = True
        return connection
    finally:
        waiting.cancel()
        if not handed_off:
            task.cancel()
        results = await asyncio.gather(task, waiting, return_exceptions=True)
        if not handed_off and isinstance(results[0], asyncssh.SSHClientConnection):
            results[0].abort()


async def connect_embedded_ssh(
    plan: EmbeddedSshPlan,
    session_id: UUID,
    password_provider: EmbeddedPasswordProvider,
    *,
    passphrase_provider: EmbeddedPasswordProvider | None = None,
    disconnect_callback: EmbeddedDisconnectCallback | None = None,
    host_key_provider: EmbeddedHostKeyProvider | None = None,
    gateway_password_provider: EmbeddedPasswordProvider | None = None,
    gateway_host_key_provider: EmbeddedHostKeyProvider | None = None,
    gateway_authentication_id: UUID | None = None,
    gateway_ready_callback=None,
    gateway_disconnect_callback=None,
) -> EmbeddedSshConnection:
    if type(plan) is not EmbeddedSshPlan:
        raise EmbeddedSshError(EmbeddedSshFailure.CONFIGURATION)
    arguments = dict(
        passphrase_provider=passphrase_provider,
        disconnect_callback=disconnect_callback,
        host_key_provider=host_key_provider,
    )
    if plan.profile.ssh_jump.mode is SshJumpMode.DIRECT:
        return await _connect_single_embedded_ssh(plan, session_id, password_provider, **arguments)
    from termana.terminal.embedded_ssh_route import connect_jump_ssh

    return await connect_jump_ssh(
        plan,
        session_id,
        password_provider,
        gateway_password_provider,
        arguments,
        gateway_host_key_provider=gateway_host_key_provider,
        gateway_authentication_id=gateway_authentication_id,
        gateway_ready_callback=gateway_ready_callback,
        gateway_disconnect_callback=gateway_disconnect_callback,
    )
