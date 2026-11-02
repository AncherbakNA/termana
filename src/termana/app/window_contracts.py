"""Private immutable operation contracts and helpers shared by window controllers.

No MainWindow import, user-data access or separate Qt/state ownership.
The logger keeps the pre-extraction diagnostic namespace.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final
from uuid import UUID

from PySide6.QtCore import QRunnable, QThreadPool

from termana.app.background_operation import BackgroundOperation
from termana.models.connection import (
    MAX_CONNECTION_NAME_CHARACTERS,
    ConnectionCatalog,
    ConnectionProfile,
)
from termana.models.ssh_credential import (
    SshCredentialRef,
    SshEndpoint,
    validate_ssh_credential_generation,
)
from termana.models.ssh_identity import SshAuthenticationMode
from termana.ssh.dialogs.saved_ssh_logins_dialog import SavedSshLoginsDialog
from termana.ssh.embedded_preflight import embedded_preflight_guidance
from termana.ssh.host_key_approval import SshHostKeyApproval
from termana.ssh.repository import (
    SshCredentialRepository,
    SshCredentialRepositoryBusyError,
    SshCredentialRepositoryError,
)
from termana.ssh.ssh_auth import SshAuthenticationSession
from termana.storage.connection_transfer import ConnectionTransferStats

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
LOGGER = logging.getLogger("termana.app.main_window")
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
        from termana.ssh.legacy_ssh import legacy_helper_path, verify_legacy_frontend
        from termana.ssh.legacy_ssh_backend import validate_legacy_profile

        try:
            validate_legacy_profile(request.profile)
            verify_legacy_frontend(legacy_helper_path())
        except (OSError, RuntimeError, TypeError, ValueError):
            return _SshLoginLookupResult(
                _SshLoginLookupStatus.PREFLIGHT_REJECTED,
                request.endpoint,
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
                _SshLoginLookupStatus.READY,
                request.endpoint,
                explicit_credential_id=credential.credential_id,
                explicit_credential_generation=credential.generation,
                explicit_username=credential.username,
            )
        except SshCredentialRepositoryError:
            return _SshLoginLookupResult(
                _SshLoginLookupStatus.PREFLIGHT_REJECTED,
                request.endpoint,
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
