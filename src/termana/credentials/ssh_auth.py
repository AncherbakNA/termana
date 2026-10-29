"""Per-session SSH_ASKPASS broker with proof-gated password persistence."""

from __future__ import annotations

import base64
import ctypes
import hmac
import json
import os
import re
import shlex
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from functools import lru_cache
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QDialog, QWidget
from shiboken6 import isValid

from termana.credentials.askpass_helper import (
    ASKPASS_EXPECTED_PARENT_PID_ENV,
    ASKPASS_MODE_ENV,
    ASKPASS_MODE_VALUE,
    ASKPASS_PROTOCOL_ENV,
    ASKPASS_PROTOCOL_VALUE,
    ASKPASS_PROTOCOL_VERSION,
    ASKPASS_SOCKET_ENV,
    ASKPASS_TOKEN_ENV,
    MAX_PASSWORD_BYTES,
    MAX_PROMPT_BYTES,
    MAX_PROMPT_KIND_BYTES,
    MAX_REQUEST_BYTES,
    MAX_SOCKET_PATH_BYTES,
    SSH_ASKPASS_PROMPT_ENV,
    TOKEN_BYTES,
    TOKEN_CHARACTERS,
)
from termana.credentials.background_operation import ConfidentialBackgroundOperation
from termana.credentials.host_key_approval import SshHostKeyApproval
from termana.credentials.repository import (
    CredentialRemovalOutcome,
    SshCredentialPasswordSnapshot,
    SshCredentialRepository,
    SshCredentialRepositoryBusyError,
)
from termana.dialogs.ssh_auth_dialogs import (
    confirm_save_ssh_password,
    confirm_ssh_action,
)
from termana.models.ssh_cipher import validate_ssh_cipher
from termana.models.ssh_credential import (
    SshCredentialRef,
    SshEndpoint,
    validate_ssh_credential_generation,
)
from termana.models.ssh_identity import (
    SshAuthenticationMode,
    SshIdentityOptions,
    ssh_authentication_arguments,
)
from termana.models.ssh_jump import SshJumpMode, SshJumpOptions
from termana.models.ssh_options import SshConnectionOptions
from termana.terminal.command import ShellCommand
from termana.terminal.ssh_command import (
    SSH_CIPHER_ISOLATION_ARGUMENTS,
    resolve_system_ssh_program,
    ssh_known_hosts_arguments,
)
from termana.terminal.ssh_tunnel_arguments import split_profile_tunnel_arguments
from termana.terminal.ssh_tunnel_diagnostics import SshChannelFailure, channel_failure_kind
from termana.terminal.title import sanitize_terminal_title

SSH_ASKPASS_ENV: Final = "SSH_ASKPASS"
SSH_ASKPASS_REQUIRE_ENV: Final = "SSH_ASKPASS_REQUIRE"
SSH_ASKPASS_REQUIRE_FORCE: Final = "force"
ASKPASS_SOCKET_NAME: Final = "askpass.sock"
AUTH_LOG_NAME: Final = "openssh.log"
CONTROL_SOCKET_NAME: Final = "control.sock"
AUTH_LOG_POLL_INTERVAL_MS: Final = 40
MAX_AUTH_LOG_BYTES: Final = 256 * 1024
MAX_DIAGNOSTIC_BYTES: Final = 64 * 1024
MAX_LOG_LINE_BYTES: Final = 16 * 1024
MAX_POST_AUTH_LOG_READ_BYTES: Final = 64 * 1024
POST_AUTH_LOG_POLL_INTERVAL_MS: Final = 500
POST_AUTH_LOG_COMPACT_THRESHOLD_BYTES: Final = 256 * 1024
POST_AUTH_CHANNEL_STATUS_INTERVAL_SECONDS: Final = 5.0
SSH_LOG_VERBOSE_ARGUMENT: Final = (
    "LogVerbose=sshconnect2.c:ssh_userauth2():*,channels.c:channel_input_open_failure():*"
    ",clientloop.c:server_alive_check():*"
    ",packet.c:sshpkt_vfatal():*"
)
MAX_ASKPASS_REQUESTS: Final = 12
MAX_PENDING_ASKPASS_CLIENTS: Final = 16
MAX_TOTAL_ASKPASS_CLIENTS: Final = 32
ASKPASS_REQUEST_TIMEOUT_MS: Final = 3_000
# Include the newline delimiter and retain one extra byte only to detect overflow.
MAX_ASKPASS_WIRE_BYTES: Final = MAX_REQUEST_BYTES + 1
ASKPASS_CLIENT_READ_BUFFER_BYTES: Final = MAX_ASKPASS_WIRE_BYTES + 1
ASYNC_SECRET_REQUEST_TIMEOUT_SECONDS: Final = 290
SAVED_PASSWORD_BUSY_RETRY_DELAYS_SECONDS: Final = (0.02, 0.04, 0.08, 0.16, 0.30)
PASSWORD_PERSISTENCE_PROOF_TIMEOUT_SECONDS: Final = 30.0
MAX_PROCESS_ID: Final = (1 << 31) - 1
MAX_CONTROL_MASTER_CHECKS: Final = 3
CONTROL_MASTER_CHECK_DELAY_SECONDS: Final = 0.08
CONTROL_SOCKET_CONNECT_TIMEOUT_SECONDS: Final = 0.05

_AUTHENTICATED_RECORD = re.compile(
    rb"^sshconnect2\.c:ssh_userauth2\(\):\d+ "
    # OpenSSH 9.x logs (pid=...), newer versions add (bin=..., pid=...).
    # This is still only one part of proof: the exact process and mux peer
    # must independently agree before a password can be offered for saving.
    rb"\((?:bin=[^,\r\n]+, )?pid=(\d+)\): Authenticated to [^\r\n]+ "
    rb'using "([a-z0-9-]+)"\.\r?$'
)
_PASSWORD_CHANGE_PROMPTS: Final = (
    "'s old password:",
    "'s new password:",
    "retype ",
)
_ASKPASS_REQUEST_FIELDS: Final = frozenset(
    {
        "version",
        "token",
        "prompt",
        "prompt_kind",
        "helper_pid",
        "parent_pid",
        "expected_parent_pid",
    }
)
_PROTECTED_ENVIRONMENT_NAMES: Final = frozenset(
    {
        SSH_ASKPASS_ENV,
        SSH_ASKPASS_REQUIRE_ENV,
        SSH_ASKPASS_PROMPT_ENV,
        ASKPASS_MODE_ENV,
        ASKPASS_EXPECTED_PARENT_PID_ENV,
        ASKPASS_PROTOCOL_ENV,
        ASKPASS_SOCKET_ENV,
        ASKPASS_TOKEN_ENV,
    }
)


class SecretPrompt(Protocol):
    """Collect one AskPass secret for an explicitly identified auth session."""

    def __call__(
        self,
        *,
        authentication_id: UUID,
        endpoint: str,
        prompt: str,
        username: str | None,
        is_host_password: bool,
        parent: QWidget | None,
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class SshSecretRequest:
    """Describe one pending AskPass response without retaining its secret."""

    request_id: UUID
    endpoint: str
    prompt: str
    username: str | None
    is_host_password: bool
    is_host_confirmation: bool = False
    is_gateway: bool = False

    def __post_init__(self):
        if type(self.is_gateway) is not bool:
            raise TypeError("SSH gateway role must be a boolean")


@dataclass(frozen=True, slots=True)
class _PendingSecretRequest:
    """Bind one public request to its authenticated helper connection."""

    request: SshSecretRequest
    client: QLocalSocket
    direct_request: bool
    password_username: str | None


@dataclass(frozen=True, slots=True)
class _PendingCredentialRead:
    """Own one exact password lookup without placing its result in a Qt signal."""

    operation_id: UUID
    task: ConfidentialBackgroundOperation
    client: QLocalSocket
    prompt: str
    direct_request: bool
    password_username: str


class _PostAuthCredentialOperationKind(Enum):
    """Identify a coordinated repository mutation without carrying its values."""

    SAVE_PASSWORD = auto()
    SAVE_ACCOUNT = auto()
    REMOVE_ACCOUNT = auto()
    REMOVE_OUTDATED_PASSWORD = auto()


@dataclass(slots=True)
class _PendingPostAuthCredentialOperation:
    """Bind one non-secret completion identity to its originating auth session."""

    operation_id: UUID
    kind: _PostAuthCredentialOperationKind
    username: str | None = None
    completed: bool = False


@dataclass(frozen=True, slots=True)
class _CredentialMutationTaskContext:
    """Retain one detached task and only the metadata needed to validate it."""

    task: ConfidentialBackgroundOperation
    kind: _PostAuthCredentialOperationKind
    endpoint: SshEndpoint | None = None
    username: str | None = None


class SshCredentialMutationOutcome(Enum):
    """Describe one non-secret, generic result delivered to its matching owner."""

    SAVE_SUCCEEDED = auto()
    REMOVE_SUCCEEDED = auto()
    REMOVE_MISSING = auto()
    REMOVE_GENERATION_CHANGED = auto()
    FAILED = auto()


ConfirmationPrompt = Callable[..., bool]
CredentialOperationStarter = Callable[[QRunnable], None]
ControlMasterCheck = Callable[[str, int], bool]
ProcessExecutableCheck = Callable[[int, str], bool]
ProcessParentCheck = Callable[[int, int], bool]


class SshAuthenticationUnavailableError(RuntimeError):
    """Raised when the private AskPass transport cannot be prepared safely."""


class _MacProcessInfo(ctypes.Structure):
    """Darwin's public PROC_PIDTBSDINFO layout through pbi_start_tvusec."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _process_executable(process_id: int) -> str | None:
    """Read one live process executable through the platform kernel interface."""
    if sys.platform == "linux":
        try:
            return os.readlink(f"/proc/{process_id}/exe")
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            function = library.proc_pidpath
            function.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
            function.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(4096)
            length = function(process_id, buffer, len(buffer))
        except (AttributeError, OSError):
            return None
        if length <= 0:
            return None
        return os.fsdecode(buffer.value)
    return None


def _process_parent_id(process_id: int) -> int | None:
    """Read a live process's real parent PID without trusting helper payloads."""
    if sys.platform == "linux":
        try:
            stat_line = Path(f"/proc/{process_id}/stat").read_text()
            fields_after_name = stat_line[stat_line.rindex(")") + 2 :].split()
            parent_id = int(fields_after_name[1])
        except (OSError, ValueError, IndexError):
            return None
        return parent_id if 0 < parent_id <= MAX_PROCESS_ID else None
    if sys.platform == "darwin":
        try:
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            function = library.proc_pidinfo
            function.argtypes = (
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            )
            function.restype = ctypes.c_int
            process_info = _MacProcessInfo()
            returned = function(
                process_id,
                3,
                0,
                ctypes.byref(process_info),
                ctypes.sizeof(process_info),
            )
        except (AttributeError, OSError):
            return None
        if returned != ctypes.sizeof(process_info):
            return None
        parent_id = int(process_info.pbi_ppid)
        return parent_id if 0 < parent_id <= MAX_PROCESS_ID else None
    return None


def _process_parent_matches(process_id: int, expected_parent_id: int) -> bool:
    return _process_parent_id(process_id) == expected_parent_id


def _unix_peer_credentials(peer_socket: socket.socket) -> tuple[int, int] | None:
    """Return kernel-authenticated (uid, pid) for a connected local socket."""
    try:
        if sys.platform == "linux" and hasattr(socket, "SO_PEERCRED"):
            raw = peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            peer_pid, peer_uid, _peer_gid = struct.unpack("3i", raw)
            return peer_uid, peer_pid
        if sys.platform == "darwin":
            local_peer_credential = peer_socket.getsockopt(0, 1, 12)
            _version, peer_uid, _group_count = struct.unpack("3i", local_peer_credential)
            peer_pid = struct.unpack("i", peer_socket.getsockopt(0, 2, 4))[0]
            return peer_uid, peer_pid
    except (OSError, struct.error):
        return None
    return None


def _process_executable_matches(process_id: int, expected_program: str) -> bool:
    actual_program = _process_executable(process_id)
    if actual_program is None:
        return False
    try:
        return os.path.samefile(actual_program, expected_program)
    except OSError:
        return False


def _script_interpreter(program: str) -> str:
    """Resolve the executable kernel identity behind an installed console script."""
    try:
        with open(program, "rb") as script:
            first_line = script.readline(8193)
            if first_line == b"#!/bin/sh\n":
                # pip/distlib uses this exact exec trampoline for Python paths
                # containing spaces or too long for a direct shebang. Never
                # evaluate shell syntax or accept another interpreter as proof.
                second_line = script.readline(8193)
                third_line = script.readline(8193)
                prefix, suffix = b"'''exec' ", b' "$0" "$@"\n'
                if (
                    len(second_line) > 8192
                    or not second_line.startswith(prefix)
                    or not second_line.endswith(suffix)
                    or third_line != b"' '''\n"
                ):
                    return program
                literal = second_line[len(prefix) : -len(suffix)]
                if literal.startswith(b'"') and literal.endswith(b'"'):
                    literal = literal[1:-1]
                    forbidden = b'"$`\\\x00\r\n'
                else:
                    forbidden = b" \t\"'`$\\;|&<>()*?[]#~\x00\r\n"
                if not literal or any(character in literal for character in forbidden):
                    return program
                interpreter = os.fsdecode(literal)
                if os.path.isabs(interpreter) and os.path.samefile(interpreter, sys.executable):
                    return interpreter
                return program
    except OSError:
        return program
    if len(first_line) > 8192 or not first_line.startswith(b"#!"):
        return program
    try:
        interpreter = os.fsdecode(first_line[2:].strip().split(maxsplit=1)[0])
    except (IndexError, UnicodeError):
        return program
    return interpreter if os.path.isabs(interpreter) else program


def _askpass_process_executable(program: str) -> str:
    """Map the installed helper script to the executable identity seen by the kernel."""
    interpreter = _script_interpreter(program)
    try:
        uses_current_runtime = os.path.samefile(interpreter, sys.executable)
    except OSError:
        uses_current_runtime = False
    if uses_current_runtime:
        return _process_executable(os.getpid()) or interpreter
    return interpreter


def _display_endpoint(endpoint: SshEndpoint) -> str:
    host = endpoint.host
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{endpoint.port}"


def _read_saved_password_with_busy_retry(
    repository: SshCredentialRepository,
    credential_id: UUID,
    endpoint: SshEndpoint,
) -> SshCredentialPasswordSnapshot | None:
    """Retry only transient repository-lock contention within a bounded worker budget."""
    for delay_seconds in SAVED_PASSWORD_BUSY_RETRY_DELAYS_SECONDS:
        try:
            return repository.password_for_with_generation(credential_id, endpoint)
        except SshCredentialRepositoryBusyError:
            time.sleep(delay_seconds)
    return repository.password_for_with_generation(credential_id, endpoint)


def _save_password_with_busy_retry(
    repository: SshCredentialRepository,
    endpoint: SshEndpoint,
    username: str,
    password: str,
) -> SshCredentialRef:
    """Retry only transient repository contention outside the GUI thread."""
    for delay_seconds in SAVED_PASSWORD_BUSY_RETRY_DELAYS_SECONDS:
        try:
            return repository.save_password(endpoint, username, password)
        except SshCredentialRepositoryBusyError:
            time.sleep(delay_seconds)
    return repository.save_password(endpoint, username, password)


def _remove_credential_with_busy_retry(
    repository: SshCredentialRepository,
    credential_id: UUID,
    expected_generation: int,
) -> CredentialRemovalOutcome:
    """Retry an exact-revision removal after short lock contention."""
    for delay_seconds in SAVED_PASSWORD_BUSY_RETRY_DELAYS_SECONDS:
        try:
            return repository.remove_credential_if_generation_matches(
                credential_id,
                expected_generation,
            )
        except SshCredentialRepositoryBusyError:
            time.sleep(delay_seconds)
    return repository.remove_credential_if_generation_matches(
        credential_id,
        expected_generation,
    )


class SshCredentialMutationCoordinator(QObject):
    """Own consent/proof-gated credential writes beyond an SSH session's lifetime."""

    operation_finished = Signal(object, object)
    pending_count_changed = Signal(int)

    def __init__(
        self,
        operation_starter: CredentialOperationStarter | None = None,
        parent: QObject | None = None,
    ) -> None:
        """Bind one stable GUI owner to an injectable worker dispatcher."""
        super().__init__(parent)
        if operation_starter is not None and not callable(operation_starter):
            raise TypeError("operation_starter must be callable")
        self._operation_starter = (
            QThreadPool.globalInstance().start if operation_starter is None else operation_starter
        )
        self._operations: dict[UUID, _CredentialMutationTaskContext] = {}

    @property
    def has_pending_operations(self) -> bool:
        """Return whether an authorized credential transaction is still retained."""
        return bool(self._operations)

    def start_account_change(self, operation_id, repository, account, password=None, *, remove=False):
        """Retain an explicitly consented account edit through application shutdown."""
        from termana.models.ssh_account import SshAccount

        if (not isinstance(operation_id, UUID) or operation_id in self._operations
                or not isinstance(repository, SshCredentialRepository)
                or type(account) is not SshAccount):
            raise ValueError("Invalid SSH account operation")
        kind = (_PostAuthCredentialOperationKind.REMOVE_ACCOUNT if remove else
                _PostAuthCredentialOperationKind.SAVE_ACCOUNT)
        def change():
            if remove:
                repository.remove_account(account)
            else:
                repository.save_account(account, password)
            return True
        self._start_operation(operation_id, _CredentialMutationTaskContext(
            task=ConfidentialBackgroundOperation(operation_id, change), kind=kind
        ))

    def start_password_save(
        self,
        operation_id: UUID,
        repository: SshCredentialRepository,
        endpoint: SshEndpoint,
        username: str,
        password: str,
    ) -> None:
        """Commit one consented password without retaining its auth-session QObject."""
        if not isinstance(operation_id, UUID) or operation_id in self._operations:
            raise ValueError("Credential mutation ID must be a unique UUID")
        if not isinstance(repository, SshCredentialRepository):
            raise TypeError("repository must be an SshCredentialRepository")
        if not isinstance(endpoint, SshEndpoint):
            raise TypeError("endpoint must be an SshEndpoint")
        if not isinstance(username, str) or not username:
            raise ValueError("username must be non-empty text")
        if not isinstance(password, str):
            raise TypeError("password must be text")

        def save_password(
            repository_snapshot: SshCredentialRepository = repository,
            endpoint_snapshot: SshEndpoint = endpoint,
            username_snapshot: str = username,
            secret: str = password,
        ) -> SshCredentialRef:
            return _save_password_with_busy_retry(
                repository_snapshot,
                endpoint_snapshot,
                username_snapshot,
                secret,
            )

        task = ConfidentialBackgroundOperation(operation_id, save_password)
        password = ""
        self._start_operation(
            operation_id,
            _CredentialMutationTaskContext(
                task=task,
                kind=_PostAuthCredentialOperationKind.SAVE_PASSWORD,
                endpoint=endpoint,
                username=username,
            ),
        )

    def start_outdated_password_removal(
        self,
        operation_id: UUID,
        repository: SshCredentialRepository,
        credential_id: UUID,
        expected_generation: int,
    ) -> None:
        """Commit one proof-authorized obsolete credential removal."""
        self._start_versioned_removal(operation_id, repository, credential_id, expected_generation)

    def start_confirmed_login_removal(
        self,
        operation_id: UUID,
        repository: SshCredentialRepository,
        credential_id: UUID,
        expected_generation: int,
    ) -> None:
        """Commit one explicitly confirmed removal of the displayed revision."""
        self._start_versioned_removal(operation_id, repository, credential_id, expected_generation)

    def _start_versioned_removal(
        self,
        operation_id: UUID,
        repository: SshCredentialRepository,
        credential_id: UUID,
        expected_generation: int,
    ) -> None:
        """Share the guarded transaction, never infer its caller's authorization."""
        if not isinstance(operation_id, UUID) or operation_id in self._operations:
            raise ValueError("Credential mutation ID must be a unique UUID")
        if not isinstance(repository, SshCredentialRepository):
            raise TypeError("repository must be an SshCredentialRepository")
        if not isinstance(credential_id, UUID):
            raise TypeError("credential_id must be a UUID")
        validate_ssh_credential_generation(expected_generation)

        task = ConfidentialBackgroundOperation(
            operation_id,
            lambda repository_snapshot=repository, credential_snapshot=credential_id, generation_snapshot=expected_generation: (
                _remove_credential_with_busy_retry(
                    repository_snapshot,
                    credential_snapshot,
                    generation_snapshot,
                )
            ),
        )
        self._start_operation(
            operation_id,
            _CredentialMutationTaskContext(
                task=task,
                kind=_PostAuthCredentialOperationKind.REMOVE_OUTDATED_PASSWORD,
            ),
        )

    def _start_operation(
        self,
        operation_id: UUID,
        context: _CredentialMutationTaskContext,
    ) -> None:
        """Retain one task before dispatch and settle starter failures exactly once."""
        task = context.task
        self._operations[operation_id] = context
        task.signals.succeeded.connect(self._handle_operation_succeeded)
        task.signals.failed.connect(self._handle_operation_failed)
        self.pending_count_changed.emit(len(self._operations))
        try:
            self._operation_starter(task)
        except Exception:
            active = self._operations.pop(operation_id, None)
            if active is None:
                return
            task.abandon(operation_id)
            self.pending_count_changed.emit(len(self._operations))
            self.operation_finished.emit(
                operation_id,
                SshCredentialMutationOutcome.FAILED,
            )

    @Slot(object)
    def _handle_operation_succeeded(self, operation_id: object) -> None:
        """Consume a one-shot result before publishing a non-secret outcome."""
        context = self._take_operation(operation_id)
        if context is None:
            return
        outcome = SshCredentialMutationOutcome.FAILED
        result: object = None
        try:
            result = context.task.take_result(operation_id)
            if context.kind in (_PostAuthCredentialOperationKind.SAVE_ACCOUNT,
                                _PostAuthCredentialOperationKind.REMOVE_ACCOUNT) and result is True:
                outcome = (SshCredentialMutationOutcome.SAVE_SUCCEEDED
                           if context.kind is _PostAuthCredentialOperationKind.SAVE_ACCOUNT
                           else SshCredentialMutationOutcome.REMOVE_SUCCEEDED)
            if context.kind is _PostAuthCredentialOperationKind.SAVE_PASSWORD:
                if (
                    isinstance(result, SshCredentialRef)
                    and result.endpoint == context.endpoint
                    and result.username == context.username
                ):
                    outcome = SshCredentialMutationOutcome.SAVE_SUCCEEDED
            elif (
                context.kind is _PostAuthCredentialOperationKind.REMOVE_OUTDATED_PASSWORD
                and isinstance(result, CredentialRemovalOutcome)
            ):
                if result is CredentialRemovalOutcome.REMOVED:
                    outcome = SshCredentialMutationOutcome.REMOVE_SUCCEEDED
                elif result is CredentialRemovalOutcome.MISSING:
                    outcome = SshCredentialMutationOutcome.REMOVE_MISSING
                elif result is CredentialRemovalOutcome.GENERATION_CHANGED:
                    outcome = SshCredentialMutationOutcome.REMOVE_GENERATION_CHANGED
        except (RuntimeError, ValueError):
            pass
        finally:
            result = None
        self.operation_finished.emit(operation_id, outcome)

    @Slot(object)
    def _handle_operation_failed(self, operation_id: object) -> None:
        """Reduce a private repository failure to one generic opaque outcome."""
        if self._take_operation(operation_id) is None:
            return
        self.operation_finished.emit(
            operation_id,
            SshCredentialMutationOutcome.FAILED,
        )

    def _take_operation(
        self,
        operation_id: object,
    ) -> _CredentialMutationTaskContext | None:
        """Consume exactly one task and publish the remaining retained count."""
        if not isinstance(operation_id, UUID):
            return None
        context = self._operations.pop(operation_id, None)
        if context is not None:
            self.pending_count_changed.emit(len(self._operations))
        return context


def resolve_askpass_program() -> str | None:
    """Resolve only the installed helper beside the active Python interpreter."""
    if sys.platform not in {"darwin", "linux"}:
        return None
    from termana.helper_runtime import bundled_helper_path, is_bundled

    if is_bundled():
        try:
            candidates = (bundled_helper_path(),)
        except RuntimeError:
            return None
    else:
        candidates = (Path(sys.executable).with_name("termana-askpass"),)

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        try:
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid in {0, os.getuid()}
            and metadata.st_mode & 0o022 == 0
            and os.access(resolved, os.X_OK)
        ):
            return str(resolved)
    return None


@lru_cache(maxsize=4)
def _openssh_supports_authentication_proof(ssh_program: str) -> bool:
    """Probe the exact LogVerbose directive used for the local auth proof."""
    if sys.platform not in {"darwin", "linux"} or not os.path.isabs(ssh_program):
        return False
    try:
        version = subprocess.run(
            (ssh_program, "-V"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=2,
        )
        version_match = re.search(
            rb"OpenSSH_(\d+)\.(\d+)",
            version.stdout + version.stderr,
        )
        if version_match is None or tuple(map(int, version_match.groups())) < (8, 7):
            return False
        completed = subprocess.run(
            (
                ssh_program,
                "-G",
                "-F",
                "/dev/null",
                "-o",
                SSH_LOG_VERBOSE_ARGUMENT,
                "localhost",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def create_ssh_authentication_session(
    *,
    repository: SshCredentialRepository,
    endpoint: SshEndpoint,
    username: str | None,
    credential_id: UUID | None,
    credential_generation: int | None,
    profile_id: UUID | None,
    ssh_program: str,
    parent: QWidget,
    secret_prompt: SecretPrompt | None = None,
    credential_operation_starter: CredentialOperationStarter | None = None,
    credential_mutation_coordinator: SshCredentialMutationCoordinator,
) -> SshAuthenticationSession | None:
    """Create a secure broker only for the trusted system OpenSSH client."""
    # The current peer-PID/control-socket proof is Unix-specific. ConPTY gives
    # Windows a real terminal, not permission to weaken password-saving proof.
    if sys.platform not in {"darwin", "linux"}:
        return None
    trusted_program = resolve_system_ssh_program()
    askpass_program = resolve_askpass_program()
    if trusted_program is None or askpass_program is None:
        return None
    try:
        if not os.path.samefile(ssh_program, trusted_program):
            return None
    except OSError:
        return None
    if not _openssh_supports_authentication_proof(trusted_program):
        return None
    try:
        return SshAuthenticationSession(
            repository=repository,
            endpoint=endpoint,
            username=username,
            credential_id=credential_id,
            credential_generation=credential_generation,
            profile_id=profile_id,
            ssh_program=trusted_program,
            askpass_program=askpass_program,
            parent=parent,
            secret_prompt=secret_prompt,
            credential_operation_starter=credential_operation_starter,
            credential_mutation_coordinator=credential_mutation_coordinator,
        )
    except SshAuthenticationUnavailableError:
        return None


def _is_canonical_profile_jump(value: str) -> bool:
    """Recognize exactly one model-formatted gateway, without OpenSSH expansion."""
    username, separator, authority = value.rpartition("@")
    if not separator:
        username, authority = None, value
    if authority.startswith("["):
        host, closing, suffix = authority[1:].partition("]")
        if not closing or (suffix and not suffix.startswith(":")):
            return False
        port_text = suffix[1:] if suffix else None
    else:
        host, colon, suffix = authority.partition(":")
        port_text = suffix if colon else None
    port = None
    if port_text is not None:
        if (
            not port_text.isascii()
            or not port_text.isdecimal()
            or len(port_text) > 5
            or str(int(port_text)) != port_text
        ):
            return False
        port = int(port_text)
    try:
        jump = SshJumpOptions(mode=SshJumpMode.JUMP, host=host, port=port, username=username)
    except ValueError:
        return False
    return jump.proxy_jump_value == value


def _without_profile_connection_isolation(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Recognize only the builder's canonical, validated isolation suffix."""
    isolation_length = len(SSH_CIPHER_ISOLATION_ARGUMENTS)
    if arguments[-isolation_length:] != SSH_CIPHER_ISOLATION_ARGUMENTS:
        return arguments
    explicit_arguments, has_tunnels = split_profile_tunnel_arguments(arguments[:-isolation_length])
    has_route = False
    if explicit_arguments[-4:] == ("-o", "ProxyJump=none", "-o", "ProxyCommand=none"):
        has_route = True
        explicit_arguments = explicit_arguments[:-4]
    elif len(explicit_arguments) >= 2 and explicit_arguments[-2] == "-J":
        if not _is_canonical_profile_jump(explicit_arguments[-1]):
            return arguments
        has_route = True
        explicit_arguments = explicit_arguments[:-2]
    # Match the builder's optional connection prefix before recognizing every
    # override. Unknown options anywhere must not authorize removing isolation.
    for flag in ("-p", "-l"):
        if len(explicit_arguments) >= 2 and explicit_arguments[0] == flag:
            explicit_arguments = explicit_arguments[2:]
    option_fields = {
        "ConnectTimeout": "connect_timeout",
        "ServerAliveInterval": "server_alive_interval",
        "ServerAliveCountMax": "server_alive_count_max",
    }
    option_values: dict[str, int] = {}
    identity_values: dict[str, str | bool] = {}
    authentication_arguments: list[str] = []
    canonical_names = (
        "SystemConfig",
        "TrustFile",
        "Cipher",
        *option_fields,
        "IdentityFile",
        "IdentitiesOnly",
        "IdentityAgent",
        "PreferredAuthentications",
        "PubkeyAuthentication",
        "PasswordAuthentication",
        "KbdInteractiveAuthentication",
    )
    names: list[str] = []
    cipher: str | None = None
    if len(explicit_arguments) % 2:
        return arguments
    for index in range(0, len(explicit_arguments), 2):
        flag, argument = explicit_arguments[index : index + 2]
        if flag == "-F":
            name, value = 'SystemConfig', argument
        elif flag == "-o" and argument.startswith('UserKnownHostsFile='):
            name, value = 'TrustFile', argument.partition('=')[2]
        elif flag == "-c":
            name, value = "Cipher", argument
        elif flag == "-i":
            name, value = "IdentityFile", argument
        elif flag == "-o":
            name, separator, value = argument.partition("=")
            if separator != "=" or name in ("Cipher", "IdentityFile"):
                return arguments
        else:
            return arguments
        if name not in canonical_names or name in names:
            return arguments
        names.append(name)
        if name == 'SystemConfig':
            if re.fullmatch(r'ssh-[0-9a-f]{64}\.conf', value) is None:
                return arguments
        elif name == 'TrustFile':
            try:
                tokens = shlex.split(value)
                if (len(tokens) != 1 or ssh_known_hosts_arguments(Path(tokens[0].replace('%%','%')))[1] != argument):
                    return arguments
            except (ValueError, TypeError):
                return arguments
        elif name == "Cipher":
            cipher = value
        elif name in option_fields:
            # Bounded integers only: no alternate spellings or coercions.
            if (
                not value.isascii()
                or not value.isdecimal()
                or len(value) > 4
                or str(int(value)) != value
            ):
                return arguments
            option_values[option_fields[name]] = int(value)
        elif name == "IdentityFile":
            identity_values["identity_file"] = value
        elif name == "IdentitiesOnly":
            if value not in ("yes", "no"):
                return arguments
            identity_values["identities_only"] = value == "yes"
        elif name == "IdentityAgent":
            if value != "none":
                return arguments
            identity_values["agent_disabled"] = True
        else:
            authentication_arguments.extend(("-o", f"{name}={value}"))
            if name == "PreferredAuthentications":
                if value not in ("publickey", "password"):
                    return arguments
                identity_values["authentication"] = (
                    SshAuthenticationMode.KEY_ONLY
                    if value == "publickey"
                    else SshAuthenticationMode.PASSWORD_ONLY
                )
    if names != [name for name in canonical_names if name in names]:
        return arguments
    try:
        options = SshConnectionOptions(**option_values)
        identity = SshIdentityOptions(**identity_values)
    except ValueError:
        return arguments
    if tuple(authentication_arguments) != ssh_authentication_arguments(identity.authentication):
        return arguments
    has_cipher = cipher is not None
    if has_cipher and validate_ssh_cipher(cipher) is None:
        raise ValueError("An explicit SSH cipher is required for session isolation")
    if (
        not has_cipher
        and options.is_default
        and identity.is_default
        and not has_route
        and not has_tunnels
    ):
        return arguments
    # A fresh private socket cannot reuse an old connection, and is needed to
    # prove authentication before saving a password. Keep all profile overrides.
    return arguments[:-isolation_length]


class SshAuthenticationSession(QObject):
    """Own one local AskPass capability and its proof-bearing OpenSSH log."""

    cancel_requested = Signal()
    secret_requested = Signal(object)
    secret_request_finished = Signal(object)
    status_message = Signal(str)
    channel_open_failed = Signal(object)

    def __init__(
        self,
        *,
        repository: SshCredentialRepository,
        endpoint: SshEndpoint,
        username: str | None,
        credential_id: UUID | None,
        credential_generation: int | None,
        profile_id: UUID | None,
        ssh_program: str,
        askpass_program: str,
        parent: QWidget | None = None,
        secret_prompt: SecretPrompt | None = None,
        confirmation_prompt: ConfirmationPrompt = confirm_ssh_action,
        save_confirmation_prompt: ConfirmationPrompt = confirm_save_ssh_password,
        control_master_check: ControlMasterCheck | None = None,
        process_executable_check: ProcessExecutableCheck = _process_executable_matches,
        process_parent_check: ProcessParentCheck = _process_parent_matches,
        credential_operation_starter: CredentialOperationStarter | None = None,
        credential_mutation_coordinator: SshCredentialMutationCoordinator,
    ) -> None:
        """Allocate private IPC, log, and control paths before starting OpenSSH."""
        super().__init__(parent)
        if not isinstance(repository, SshCredentialRepository):
            raise TypeError("repository must be an SshCredentialRepository")
        if not isinstance(endpoint, SshEndpoint):
            raise TypeError("endpoint must be an SshEndpoint")
        if username is not None and (not isinstance(username, str) or not username):
            raise ValueError("username must be non-empty text or None")
        if credential_id is not None and not isinstance(credential_id, UUID):
            raise TypeError("credential_id must be a UUID or None")
        if credential_generation is not None:
            validate_ssh_credential_generation(credential_generation)
        if (credential_id is None) != (credential_generation is None):
            raise ValueError("credential_id and credential_generation must be provided together")
        if profile_id is not None and not isinstance(profile_id, UUID):
            raise TypeError("profile_id must be a UUID or None")
        for program in (ssh_program, askpass_program):
            if not isinstance(program, str) or not os.path.isabs(program):
                raise SshAuthenticationUnavailableError(
                    "SSH authentication helpers must use absolute paths"
                )
        if credential_operation_starter is not None and not callable(credential_operation_starter):
            raise TypeError("credential_operation_starter must be callable")
        if not isinstance(
            credential_mutation_coordinator,
            SshCredentialMutationCoordinator,
        ):
            raise TypeError(
                "credential_mutation_coordinator must be an SshCredentialMutationCoordinator"
            )

        self.authentication_id = uuid4()
        self._repository = repository
        self._endpoint = endpoint
        self._username = username
        self._credential_id = credential_id
        self._credential_generation = credential_generation
        self._profile_id = profile_id
        self._ssh_program = ssh_program
        self._askpass_program = askpass_program
        self._helper_executable = _askpass_process_executable(askpass_program)
        self._dialog_parent = parent
        self._confirmation_dialog: QDialog | None = None
        self._secret_prompt = secret_prompt
        self._host_key_approval: SshHostKeyApproval | None = None
        self._host_key_approval_configured = False
        self._host_key_approval_rejected = False
        self._confirmation_prompt = confirmation_prompt
        self._save_confirmation_prompt = save_confirmation_prompt
        self._control_master_check = control_master_check or self._check_control_master
        self._process_executable_check = process_executable_check
        self._process_parent_check = process_parent_check
        self._credential_operation_starter = (
            QThreadPool.globalInstance().start
            if credential_operation_starter is None
            else credential_operation_starter
        )
        self._credential_mutation_coordinator = credential_mutation_coordinator
        self._credential_mutation_coordinator.operation_finished.connect(
            self._handle_post_auth_credential_operation_finished
        )
        self._token = base64.urlsafe_b64encode(os.urandom(TOKEN_BYTES)).rstrip(b"=").decode()
        self._runtime_directory = ""
        self._askpass_socket_path = ""
        self._auth_log_path = ""
        self._control_socket_path = ""
        self._auth_log_fd = -1
        self._auth_log_offset = 0
        self._auth_log_bytes_seen = 0
        self._diagnostics = bytearray()
        self._pending_log_line = bytearray()
        self._pending_post_auth_log_line = bytearray()
        self._discard_post_auth_log_line = False
        self._post_auth_diagnostics_unavailable = False
        self._post_auth_log_omission_reported = False
        self._next_channel_failure_status_at = 0.0
        self._last_channel_failure_kind = SshChannelFailure.GENERIC
        self._log_trust_lost = False
        self._auth_records: set[tuple[int, str]] = set()
        self._expected_ssh_pid: int | None = None
        self._candidate_password: str | None = None
        self._candidate_username: str | None = None
        self._candidate_expires_at = 0.0
        self._persistence_proof_disabled = False
        self._password_change_seen = False
        self._saved_password_used = False
        self._autofill_attempted = False
        self._authentication_finalized = False
        self._control_master_check_attempts = 0
        self._next_control_master_check_at = 0.0
        self._request_count = 0
        self._accepted_client_count = 0
        self._served_helper_pids: set[int] = set()
        self._active_request = False
        self._pending_secret_request: _PendingSecretRequest | None = None
        self._pending_credential_read: _PendingCredentialRead | None = None
        self._post_auth_credential_operations: dict[
            UUID,
            _PendingPostAuthCredentialOperation,
        ] = {}
        self._closed = False
        self._client_buffers: dict[QLocalSocket, bytearray] = {}
        self._client_peers: dict[QLocalSocket, tuple[int, int]] = {}
        self._client_deadlines: dict[QLocalSocket, float] = {}

        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept_connections)
        self._log_timer = QTimer(self)
        self._log_timer.setInterval(AUTH_LOG_POLL_INTERVAL_MS)
        self._log_timer.timeout.connect(self._poll_authentication_log)

        try:
            self._prepare_private_paths()
            if not self._server.listen(self._askpass_socket_path):
                raise OSError(self._server.errorString())
        except OSError as error:
            self._cleanup_paths()
            raise SshAuthenticationUnavailableError(
                "The local SSH password broker could not be started"
            ) from error
        self._log_timer.start()

    @property
    def auth_log_path(self) -> str:
        """Return the non-secret private OpenSSH log path for command assembly."""
        return self._auth_log_path

    @property
    def control_socket_path(self) -> str:
        """Return the unique local control socket path used as an auth signal."""
        return self._control_socket_path

    def submit_secret(self, request_id: UUID, value: str) -> bool:
        """Complete exactly one pending inline request with a bounded secret."""
        pending = self._pending_secret_request
        if (
            self._closed
            or not isinstance(request_id, UUID)
            or pending is None
            or pending.request.request_id != request_id
        ):
            return False
        if not self._valid_askpass_value(value):
            self._finish_pending_secret_request(
                pending,
                status="cancel",
                cancel_direct=True,
            )
            return False
        if (
            pending.direct_request
            and pending.password_username is not None
            and not self._password_change_seen
        ):
            self._remember_password_candidate(value, pending.password_username)
        self._finish_pending_secret_request(
            pending,
            status="ok",
            value=value,
        )
        return True

    def cancel_secret(self, request_id: UUID) -> bool:
        """Cancel exactly one pending inline request and its direct SSH flow."""
        pending = self._pending_secret_request
        if (
            self._closed
            or not isinstance(request_id, UUID)
            or pending is None
            or pending.request.request_id != request_id
        ):
            return False
        self._finish_pending_secret_request(
            pending,
            status="cancel",
            cancel_direct=True,
        )
        return True

    def bind_ssh_process(self, process_id: int) -> None:
        """Bind this capability once to the child PID launched by the terminal backend."""
        if isinstance(process_id, bool) or not isinstance(process_id, int):
            raise TypeError("SSH process ID must be an integer")
        if not 0 < process_id <= MAX_PROCESS_ID:
            raise ValueError("SSH process ID is out of range")
        if self._closed:
            raise RuntimeError("SSH authentication session is already closed")
        if self._expected_ssh_pid is not None and self._expected_ssh_pid != process_id:
            raise RuntimeError("SSH authentication session is already bound")
        self._expected_ssh_pid = process_id

    def prepare_command(self, command: ShellCommand) -> ShellCommand:
        """Add one session's AskPass capability and proof options to OpenSSH."""
        if self._closed:
            raise RuntimeError("SSH authentication session is already closed")
        if command.program != self._ssh_program:
            raise ValueError("SSH command does not use this authentication session's program")
        try:
            delimiter_index = command.arguments.index("--")
        except ValueError as error:
            raise ValueError("SSH command must contain the option delimiter") from error

        protected_collision = _PROTECTED_ENVIRONMENT_NAMES.intersection(
            name for name, _value in command.environment
        )
        if protected_collision:
            raise ValueError("SSH command already contains protected AskPass environment")

        # The original command remains unchanged for manual fallback.
        connection_arguments = _without_profile_connection_isolation(
            command.arguments[:delimiter_index]
        )
        if self._credential_id is not None and any(
            connection_arguments[index : index + 2] == ("-o", "PreferredAuthentications=publickey")
            for index in range(len(connection_arguments) - 1)
        ):
            raise ValueError("Saved passwords cannot be used with key-only authentication")

        options = [
            "-E",
            self._auth_log_path,
            "-o",
            "LogLevel=ERROR",
            "-o",
            SSH_LOG_VERBOSE_ARGUMENT,
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPath={self._control_socket_path}",
            "-o",
            "ControlPersist=no",
            "-o",
            "BatchMode=no",
        ]
        if self._credential_id is not None:
            options.extend(
                (
                    "-o",
                    "PasswordAuthentication=yes",
                    "-o",
                    "PreferredAuthentications=password",
                    "-o",
                    "PubkeyAuthentication=no",
                    "-o",
                    "KbdInteractiveAuthentication=no",
                    "-o",
                    "GSSAPIAuthentication=no",
                    "-o",
                    "HostbasedAuthentication=no",
                    "-o",
                    "NumberOfPasswordPrompts=1",
                )
            )
        arguments = (
            *connection_arguments,
            *options,
            *command.arguments[delimiter_index:],
        )
        environment = (
            *command.environment,
            (SSH_ASKPASS_ENV, self._askpass_program),
            (SSH_ASKPASS_REQUIRE_ENV, SSH_ASKPASS_REQUIRE_FORCE),
            (SSH_ASKPASS_PROMPT_ENV, ""),
            (ASKPASS_MODE_ENV, ASKPASS_MODE_VALUE),
            (ASKPASS_PROTOCOL_ENV, ASKPASS_PROTOCOL_VALUE),
            (ASKPASS_SOCKET_ENV, self._askpass_socket_path),
            (ASKPASS_TOKEN_ENV, self._token),
            (ASKPASS_EXPECTED_PARENT_PID_ENV, ""),
        )
        return ShellCommand(command.program, arguments, environment, command.working_directory)

    def approve_host_key_once(self, approval: SshHostKeyApproval) -> None:
        """Arm explicit fingerprint consent before launching this fresh client."""
        if not isinstance(approval, SshHostKeyApproval) or approval.endpoint != self._endpoint:
            raise ValueError("Host-key approval must belong to this SSH endpoint")
        if self._closed or self._expected_ssh_pid is not None or self._host_key_approval_configured:
            raise RuntimeError("Host-key approval can only be armed once before launch")
        self._host_key_approval = approval
        self._host_key_approval_configured = True

    @property
    def host_key_approval_rejected(self) -> bool:
        """Expose a non-secret failure for persistent owning-tab guidance."""
        return self._host_key_approval_rejected

    def finish(self) -> bytes:
        """Drain private diagnostics, close the broker, and return a bounded copy."""
        if not self._closed:
            self._poll_authentication_log()
        diagnostics = (
            b""
            if self._log_trust_lost or self._post_auth_diagnostics_unavailable
            else bytes(self._diagnostics)
        )
        self.close()
        self._diagnostics.clear()
        return diagnostics

    def close(self) -> None:
        """Idempotently revoke the capability and remove only owned private paths."""
        if self._closed:
            return
        self._closed = True
        self._host_key_approval = None
        dialog, self._confirmation_dialog = self._confirmation_dialog, None
        if dialog is not None and isValid(dialog):
            dialog.reject()
        self._abandon_pending_saved_password_read()
        self._log_timer.stop()
        self._close_broker()
        if self._auth_log_fd >= 0:
            try:
                os.close(self._auth_log_fd)
            except OSError:
                pass
            self._auth_log_fd = -1
        self._clear_password_candidate()
        self._token = ""
        self._cleanup_paths()

    def _prepare_private_paths(self) -> None:
        runtime_directory = tempfile.mkdtemp(prefix="termana-ssh-auth-", dir="/tmp")
        self._runtime_directory = runtime_directory
        os.chmod(runtime_directory, 0o700)
        metadata = os.lstat(runtime_directory)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OSError("Unsafe SSH authentication directory")

        self._askpass_socket_path = os.path.join(runtime_directory, ASKPASS_SOCKET_NAME)
        self._auth_log_path = os.path.join(runtime_directory, AUTH_LOG_NAME)
        self._control_socket_path = os.path.join(runtime_directory, CONTROL_SOCKET_NAME)
        if len(os.fsencode(self._askpass_socket_path)) > MAX_SOCKET_PATH_BYTES:
            raise OSError("SSH authentication socket path is too long")

        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._auth_log_fd = os.open(self._auth_log_path, flags, 0o600)
        metadata = os.fstat(self._auth_log_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise OSError("Unsafe SSH authentication log")

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            client = self._server.nextPendingConnection()
            if client is None:
                continue
            client.setReadBufferSize(ASKPASS_CLIENT_READ_BUFFER_BYTES)
            self._accepted_client_count += 1
            if self._accepted_client_count > MAX_TOTAL_ASKPASS_CLIENTS:
                client.abort()
                self._close_broker()
                break
            if len(self._client_buffers) >= MAX_PENDING_ASKPASS_CLIENTS:
                client.abort()
                continue
            peer = self._kernel_peer_credentials(client)
            if peer is None or peer[0] != os.getuid():
                client.abort()
                continue
            self._client_buffers[client] = bytearray()
            self._client_peers[client] = peer
            self._client_deadlines[client] = time.monotonic() + (ASKPASS_REQUEST_TIMEOUT_MS / 1000)
            client.readyRead.connect(lambda client=client: self._read_client(client))
            client.disconnected.connect(lambda client=client: self._drop_client(client))
            self._read_client(client)

    def _read_client(self, client: QLocalSocket) -> None:
        buffer = self._client_buffers.get(client)
        if buffer is None:
            return
        remaining = ASKPASS_CLIENT_READ_BUFFER_BYTES - len(buffer)
        if remaining <= 0:
            self._reject_client(client)
            return
        buffer.extend(bytes(client.read(remaining)))
        if len(buffer) > MAX_ASKPASS_WIRE_BYTES:
            self._reject_client(client)
            return
        newline = buffer.find(b"\n")
        if newline < 0:
            if len(buffer) > MAX_REQUEST_BYTES:
                self._reject_client(client)
            return
        if newline != len(buffer) - 1:
            self._reject_client(client)
            return
        request_data = bytes(buffer[:newline])
        buffer.clear()
        self._client_deadlines.pop(client, None)
        self._handle_request(client, request_data)

    def _handle_request(self, client: QLocalSocket, data: bytes) -> None:
        peer = self._client_peers.get(client)
        request = self._decode_request(data)
        if (
            request is None
            or peer is None
            or request[0] != peer[1]
            or request[0] in self._served_helper_pids
            or not self._process_executable_check(request[0], self._helper_executable)
        ):
            self._reject_client(client)
            return
        helper_pid, parent_pid, expected_parent_pid, prompt, prompt_kind = request
        if (
            self._expected_ssh_pid is None
            or expected_parent_pid != self._expected_ssh_pid
            or not self._process_parent_check(helper_pid, parent_pid)
        ):
            self._reject_client(client)
            return
        direct_request = parent_pid == self._expected_ssh_pid
        if direct_request:
            if not self._process_executable_check(parent_pid, self._ssh_program):
                self._reject_client(client)
                return

        self._request_count += 1
        if self._request_count > MAX_ASKPASS_REQUESTS:
            self._respond(client, status="cancel")
            return
        self._served_helper_pids.add(helper_pid)
        if self._active_request:
            self._respond(client, status="cancel")
            if direct_request:
                QTimer.singleShot(0, self.cancel_requested.emit)
            return

        self._active_request = True
        try:
            if self._host_key_approval_rejected:
                self._respond(client, status="cancel")
                return
            approval = self._host_key_approval
            if approval is not None:
                # Never unlock a credential before this exact key has been
                # observed by the authenticated direct client. Repeated or
                # changed offers, helpers and unexpected prompt kinds fail closed.
                self._host_key_approval = None
                if direct_request and prompt_kind == "" and approval.matches_question(prompt):
                    self._respond(client, status="ok", value="yes")
                    self.status_message.emit(
                        "The presented key matches your approval; verifying SSH."
                    )
                else:
                    self._host_key_approval_rejected = True
                    self._respond(client, status="cancel")
                    self.status_message.emit(
                        "The SSH host key could not be matched to your approval. "
                        "No password was sent; verify the server identity before trying again."
                    )
                return
            if self._host_key_approval_configured and prompt.startswith(
                "The authenticity of host '"
            ):
                self._host_key_approval_rejected = True
                self._respond(client, status="cancel")
                return
            if prompt_kind == "none":
                self.status_message.emit("OpenSSH reported an authentication notice.")
                self._respond(client, status="ok", value="")
                return
            if prompt_kind == "confirm":
                confirmed = self._request_confirmation(
                    self._confirmation_prompt,
                    endpoint=(
                        _display_endpoint(self._endpoint)
                        if direct_request
                        else f"Connection helper for {_display_endpoint(self._endpoint)}"
                    ),
                    prompt=prompt,
                    parent=self._dialog_parent,
                )
                if self._closed or client not in self._client_peers:
                    return
                if confirmed:
                    self._respond(client, status="ok", value="")
                else:
                    self._respond(client, status="cancel")
                    if direct_request:
                        QTimer.singleShot(0, self.cancel_requested.emit)
                return
            self._answer_secret_prompt(client, prompt, direct_request=direct_request)
        finally:
            if self._pending_secret_request is None and self._pending_credential_read is None:
                self._active_request = False

    def _answer_secret_prompt(
        self,
        client: QLocalSocket,
        prompt: str,
        *,
        direct_request: bool,
    ) -> None:
        password_username = self._password_prompt_username(prompt) if direct_request else None
        prompt_folded = prompt.casefold()
        if direct_request and any(marker in prompt_folded for marker in _PASSWORD_CHANGE_PROMPTS):
            self._password_change_seen = True
            self._clear_password_candidate()
            password_username = None

        if (
            password_username is not None
            and self._credential_id is not None
            and not self._autofill_attempted
        ):
            self._autofill_attempted = True
            if self._begin_saved_password_read(
                client,
                prompt,
                direct_request=direct_request,
                password_username=password_username,
            ):
                return

        self._request_manual_secret(
            client,
            prompt,
            direct_request=direct_request,
            password_username=password_username,
        )

    def _begin_saved_password_read(
        self,
        client: QLocalSocket,
        prompt: str,
        *,
        direct_request: bool,
        password_username: str,
    ) -> bool:
        """Dispatch one exact keychain read while retaining socket affinity here."""
        credential_id = self._credential_id
        if credential_id is None or self._pending_credential_read is not None:
            return False
        operation_id = uuid4()
        repository = self._repository
        endpoint = self._endpoint
        task = ConfidentialBackgroundOperation(
            operation_id,
            lambda repository_snapshot=repository, credential_snapshot=credential_id, endpoint_snapshot=endpoint: (
                _read_saved_password_with_busy_retry(
                    repository_snapshot,
                    credential_snapshot,
                    endpoint_snapshot,
                )
            ),
        )
        pending = _PendingCredentialRead(
            operation_id=operation_id,
            task=task,
            client=client,
            prompt=prompt,
            direct_request=direct_request,
            password_username=password_username,
        )
        self._pending_credential_read = pending
        self._client_deadlines[client] = time.monotonic() + ASYNC_SECRET_REQUEST_TIMEOUT_SECONDS
        task.signals.succeeded.connect(self._handle_saved_password_read_succeeded)
        task.signals.failed.connect(self._handle_saved_password_read_failed)
        try:
            self._credential_operation_starter(task)
        except Exception:
            if self._pending_credential_read is not pending:
                # A custom/direct starter may complete the task before reporting
                # its own failure.  Its completion already answered or established
                # the one manual request, so starting another would be unsafe.
                return True
            self._pending_credential_read = None
            pending.task.abandon(pending.operation_id)
            self._client_deadlines.pop(client, None)
            self.status_message.emit(
                "The saved SSH password could not be unlocked; enter it manually."
            )
            return False
        return True

    def _abandon_pending_saved_password_read(
        self,
        client: QLocalSocket | None = None,
    ) -> bool:
        """Tombstone one exact stale lookup and return whether it owned direct SSH."""
        pending = self._pending_credential_read
        if pending is None or (client is not None and pending.client is not client):
            return False
        self._pending_credential_read = None
        self._client_deadlines.pop(pending.client, None)
        pending.task.abandon(pending.operation_id)
        if self._pending_secret_request is None:
            self._active_request = False
        return pending.direct_request

    @Slot(object)
    def _handle_saved_password_read_succeeded(self, operation_id: object) -> None:
        """Consume a secret only after the exact live GUI-owned request is verified."""
        pending = self._pending_credential_read
        if (
            not isinstance(operation_id, UUID)
            or pending is None
            or pending.operation_id != operation_id
        ):
            return
        self._pending_credential_read = None
        snapshot: object = None
        password: str | None = None
        try:
            snapshot = pending.task.take_result(operation_id)
            if self._closed or pending.client not in self._client_peers:
                self._active_request = False
                return
            if isinstance(snapshot, SshCredentialPasswordSnapshot) and self._valid_askpass_value(
                snapshot.password
            ):
                password = snapshot.password
                self._credential_generation = snapshot.generation
                self._saved_password_used = True
                self._client_deadlines.pop(pending.client, None)
                self._respond(pending.client, status="ok", value=password)
                self._active_request = False
                return
            if snapshot is not None:
                self.status_message.emit(
                    "The saved SSH password has an unsupported format; enter it manually."
                )
            self._request_manual_secret(
                pending.client,
                pending.prompt,
                direct_request=pending.direct_request,
                password_username=pending.password_username,
            )
        finally:
            password = None
            snapshot = None
            if self._pending_secret_request is None and self._pending_credential_read is None:
                self._active_request = False

    @Slot(object)
    def _handle_saved_password_read_failed(self, operation_id: object) -> None:
        """Return a still-live AskPass request to manual entry after store failure."""
        pending = self._pending_credential_read
        if (
            not isinstance(operation_id, UUID)
            or pending is None
            or pending.operation_id != operation_id
        ):
            return
        self._pending_credential_read = None
        if self._closed or pending.client not in self._client_peers:
            self._active_request = False
            return
        self.status_message.emit("The saved SSH password could not be unlocked; enter it manually.")
        self._request_manual_secret(
            pending.client,
            pending.prompt,
            direct_request=pending.direct_request,
            password_username=pending.password_username,
        )
        if self._pending_secret_request is None and self._pending_credential_read is None:
            self._active_request = False

    def _request_manual_secret(
        self,
        client: QLocalSocket,
        prompt: str,
        *,
        direct_request: bool,
        password_username: str | None,
    ) -> None:
        """Continue the existing inline or injected manual AskPass path."""

        if self._secret_prompt is None:
            self._begin_secret_request(
                client,
                prompt,
                direct_request=direct_request,
                password_username=password_username,
            )
            return

        response = self._secret_prompt(
            authentication_id=self.authentication_id,
            endpoint=_display_endpoint(self._endpoint),
            prompt=prompt,
            username=password_username or self._username,
            is_host_password=password_username is not None,
            parent=self._dialog_parent,
        )
        if self._closed or client not in self._client_peers:
            response = None
            return
        if response is None:
            self._respond(client, status="cancel")
            if direct_request:
                QTimer.singleShot(0, self.cancel_requested.emit)
            return
        if not self._valid_askpass_value(response):
            self._respond(client, status="cancel")
            if direct_request:
                QTimer.singleShot(0, self.cancel_requested.emit)
            return
        if password_username is not None and not self._password_change_seen:
            self._remember_password_candidate(response, password_username)
        self._respond(client, status="ok", value=response)
        response = None

    def _begin_secret_request(
        self,
        client: QLocalSocket,
        prompt: str,
        *,
        direct_request: bool,
        password_username: str | None,
    ) -> None:
        """Publish one non-secret request while retaining the helper connection."""
        request = SshSecretRequest(
            request_id=uuid4(),
            endpoint=_display_endpoint(self._endpoint),
            prompt=prompt,
            username=password_username or self._username,
            is_host_password=password_username is not None,
            # Only a validated direct OpenSSH AskPass request may opt into
            # visible yes/no input. Key passphrases and helper prompts stay hidden.
            is_host_confirmation=(
                direct_request
                and password_username is None
                and prompt.startswith("The authenticity of host ")
                and any(
                    marker in prompt
                    for marker in ("key fingerprint is SHA256:", "key fingerprint is: SHA256:")
                )
                and prompt.rstrip().endswith(
                    (
                        "Are you sure you want to continue connecting (yes/no/[fingerprint])?",
                        "Are you sure you want to continue connecting (yes/no)?",
                    )
                )
            ),
        )
        self._pending_secret_request = _PendingSecretRequest(
            request=request,
            client=client,
            direct_request=direct_request,
            password_username=password_username,
        )
        self._client_deadlines[client] = time.monotonic() + ASYNC_SECRET_REQUEST_TIMEOUT_SECONDS
        self.secret_requested.emit(request)

    def _finish_pending_secret_request(
        self,
        pending: _PendingSecretRequest,
        *,
        status: str | None,
        value: str | None = None,
        cancel_direct: bool = False,
    ) -> bool:
        """Clear one exact request before responding or notifying reentrant UI code."""
        if self._pending_secret_request is not pending:
            return False
        self._pending_secret_request = None
        self._active_request = False
        self._client_deadlines.pop(pending.client, None)
        should_cancel = cancel_direct and pending.direct_request and not self._closed
        try:
            if status is not None and pending.client in self._client_peers:
                self._respond(pending.client, status=status, value=value)
        finally:
            self.secret_request_finished.emit(pending.request)
            if should_cancel:
                QTimer.singleShot(0, self.cancel_requested.emit)
        return True

    def _password_prompt_username(self, prompt: str) -> str | None:
        if any(character in prompt for character in "\x00\r\n"):
            return None
        suffix = "'s password:"
        stripped = prompt.rstrip(" ")
        if not stripped.endswith(suffix):
            return None
        identity = stripped[: -len(suffix)]
        if self._username is not None:
            expected_prefix = f"{self._username}@"
            if not identity.startswith(expected_prefix) or not identity[len(expected_prefix) :]:
                return None
            return self._username
        username_fragment, separator, prompt_host = identity.rpartition("@")
        if not separator or not username_fragment or not prompt_host:
            return None
        try:
            candidate = SshCredentialRef(endpoint=self._endpoint, username=username_fragment)
        except ValueError:
            return None
        return candidate.username

    @staticmethod
    def _valid_askpass_value(value: object) -> bool:
        if not isinstance(value, str) or any(character in value for character in "\x00\r\n"):
            return False
        try:
            return len(value.encode("utf-8")) <= MAX_PASSWORD_BYTES
        except UnicodeError:
            return False

    def _decode_request(self, data: bytes) -> tuple[int, int, int, str, str] | None:
        try:
            payload = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            return None
        if not isinstance(payload, dict) or set(payload) != _ASKPASS_REQUEST_FIELDS:
            return None
        version = payload["version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != ASKPASS_PROTOCOL_VERSION
        ):
            return None
        token = payload["token"]
        if (
            not isinstance(token, str)
            or not token.isascii()
            or len(token) != TOKEN_CHARACTERS
            or not hmac.compare_digest(token, self._token)
        ):
            return None
        process_ids = (
            payload["helper_pid"],
            payload["parent_pid"],
            payload["expected_parent_pid"],
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_PROCESS_ID
            for value in process_ids
        ):
            return None
        prompt = payload["prompt"]
        prompt_kind = payload["prompt_kind"]
        if not _bounded_text(prompt, MAX_PROMPT_BYTES):
            return None
        if not _bounded_text(prompt_kind, MAX_PROMPT_KIND_BYTES):
            return None
        if prompt_kind not in {"", "confirm", "none"}:
            return None
        return (*process_ids, prompt, prompt_kind)

    def _respond(self, client: QLocalSocket, *, status: str, value: str | None = None) -> None:
        if status == "ok":
            payload = {"status": "ok", "value": "" if value is None else value}
        else:
            payload = {"status": "cancel"}
        encoded = bytearray(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        encoded.append(0x0A)
        try:
            client.write(bytes(encoded))
            client.flush()
            client.disconnectFromServer()
        finally:
            encoded[:] = b"\x00" * len(encoded)

    def _reject_client(self, client: QLocalSocket) -> None:
        client.abort()
        self._drop_client(client)

    def _drop_client(self, client: QLocalSocket) -> None:
        pending = self._pending_secret_request
        credential_pending = self._pending_credential_read
        if (
            client not in self._client_buffers
            and client not in self._client_peers
            and (pending is None or pending.client is not client)
            and (credential_pending is None or credential_pending.client is not client)
        ):
            return
        cancel_direct_credential_read = self._abandon_pending_saved_password_read(client)
        buffer = self._client_buffers.pop(client, None)
        if buffer is not None:
            buffer[:] = b"\x00" * len(buffer)
        self._client_peers.pop(client, None)
        self._client_deadlines.pop(client, None)
        if pending is not None and pending.client is client:
            self._finish_pending_secret_request(
                pending,
                status=None,
                cancel_direct=True,
            )
        client.close()
        if cancel_direct_credential_read and not self._closed:
            QTimer.singleShot(0, self.cancel_requested.emit)

    @staticmethod
    def _kernel_peer_credentials(client: QLocalSocket) -> tuple[int, int] | None:
        duplicate = -1
        try:
            descriptor = int(client.socketDescriptor())
            duplicate = os.dup(descriptor)
            peer_socket = socket.socket(fileno=duplicate)
        except (OSError, TypeError, ValueError):
            if duplicate >= 0:
                os.close(duplicate)
            return None
        try:
            return _unix_peer_credentials(peer_socket)
        finally:
            peer_socket.close()

    def _poll_authentication_log(self) -> None:
        self._expire_incomplete_clients()
        self._expire_password_candidate()
        if self._closed or self._auth_log_fd < 0 or self._log_trust_lost:
            return
        if self._authentication_finalized:
            self._poll_post_authentication_log()
            return
        try:
            metadata = os.fstat(self._auth_log_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size < self._auth_log_offset
            ):
                self._lose_authentication_log_trust()
                return
            while True:
                chunk = os.pread(self._auth_log_fd, 16 * 1024, self._auth_log_offset)
                if not chunk:
                    break
                self._auth_log_offset += len(chunk)
                self._auth_log_bytes_seen += len(chunk)
                if self._auth_log_bytes_seen > MAX_AUTH_LOG_BYTES:
                    self._lose_authentication_log_trust()
                    break
                self._append_diagnostics(chunk)
                self._parse_authentication_records(chunk)
                if self._log_trust_lost:
                    break
        except OSError:
            self._lose_authentication_log_trust()
            return
        self._maybe_finalize_authentication()

    def _post_auth_log_metadata_is_trusted(self, metadata: os.stat_result) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and metadata.st_size >= self._auth_log_offset
        )

    def _poll_post_authentication_log(self) -> None:
        """Collect bounded diagnostics after proof has irreversibly completed.

        OpenSSH opens its -E log with O_APPEND (openssh-portable/log.c,
        log_redirect_stderr_to), so truncating our owned descriptor does not
        leave a sparse gap on its next write. This limits retention between
        polls, not instantaneous disk use; concurrent writes may be omitted.
        Pre-authentication proof limits and trust-loss behavior stay unchanged.
        """
        if self._post_auth_diagnostics_unavailable:
            return
        omitted = False
        channel_failed = False
        try:
            metadata = os.fstat(self._auth_log_fd)
            if not self._post_auth_log_metadata_is_trusted(metadata):
                self._stop_post_authentication_diagnostics()
                return
            snapshot_size = metadata.st_size
            if snapshot_size - self._auth_log_offset > MAX_POST_AUTH_LOG_READ_BYTES:
                self._auth_log_offset = snapshot_size - MAX_POST_AUTH_LOG_READ_BYTES
                self._pending_post_auth_log_line.clear()
                self._discard_post_auth_log_line = True
                omitted = True
            # A size snapshot bounds work even if the writer continuously appends.
            while self._auth_log_offset < snapshot_size:
                chunk = os.pread(
                    self._auth_log_fd,
                    min(16 * 1024, snapshot_size - self._auth_log_offset),
                    self._auth_log_offset,
                )
                if not chunk:
                    self._stop_post_authentication_diagnostics()
                    return
                self._auth_log_offset += len(chunk)
                self._append_diagnostics(chunk)
                channel_failed |= self._parse_post_authentication_diagnostics(chunk)
            if snapshot_size >= POST_AUTH_LOG_COMPACT_THRESHOLD_BYTES:
                if not self._post_auth_log_metadata_is_trusted(os.fstat(self._auth_log_fd)):
                    self._stop_post_authentication_diagnostics()
                    return
                os.ftruncate(self._auth_log_fd, 0)
                self._auth_log_offset = 0
                self._pending_post_auth_log_line.clear()
                self._discard_post_auth_log_line = False
                omitted = True
        except OSError:
            self._stop_post_authentication_diagnostics()
            return
        if omitted and not self._post_auth_log_omission_reported:
            self._post_auth_log_omission_reported = True
            self._emit_post_authentication_status(
                "Some diagnostics were omitted to limit log retention."
            )
        if channel_failed and not self._closed:
            self._report_channel_open_failure()

    def _parse_post_authentication_diagnostics(self, chunk: bytes) -> bool:
        """Recognize only a failure category; never trust log text as proof or UI text."""
        self._pending_post_auth_log_line.extend(chunk)
        failed = False
        while b"\n" in self._pending_post_auth_log_line:
            line, _, remainder = self._pending_post_auth_log_line.partition(b"\n")
            self._pending_post_auth_log_line = bytearray(remainder)
            if self._discard_post_auth_log_line:
                self._discard_post_auth_log_line = False
                continue
            if len(line) <= MAX_LOG_LINE_BYTES:
                kind = channel_failure_kind(line)
                if kind is not None:
                    self._last_channel_failure_kind = kind
                    failed = True
        if len(self._pending_post_auth_log_line) > MAX_LOG_LINE_BYTES:
            self._pending_post_auth_log_line.clear()
            self._discard_post_auth_log_line = True
        return failed

    def _report_channel_open_failure(self) -> None:
        now = time.monotonic()
        if now < self._next_channel_failure_status_at:
            return
        self._next_channel_failure_status_at = now + POST_AUTH_CHANNEL_STATUS_INTERVAL_SECONDS
        self._emit_post_authentication_status(
            "A channel could not be opened. Check the destination service and SSH permissions."
        )
        if not self._closed:
            self.channel_open_failed.emit(self._last_channel_failure_kind)

    def _emit_post_authentication_status(self, message: str) -> None:
        if not self._closed:
            endpoint = sanitize_terminal_title(_display_endpoint(self._endpoint))
            self.status_message.emit(f"SSH {endpoint}: {message}")

    def _stop_post_authentication_diagnostics(self) -> None:
        """Stop unsafe collection without retracting completed authentication proof."""
        self._post_auth_diagnostics_unavailable = True
        self._pending_post_auth_log_line.clear()
        self._log_timer.stop()
        self._emit_post_authentication_status(
            "Further diagnostics are unavailable for this session."
        )

    def _expire_incomplete_clients(self) -> None:
        now = time.monotonic()
        expired = [client for client, deadline in self._client_deadlines.items() if deadline <= now]
        for client in expired:
            self._reject_client(client)

    def _append_diagnostics(self, chunk: bytes) -> None:
        self._diagnostics.extend(chunk)
        overflow = len(self._diagnostics) - MAX_DIAGNOSTIC_BYTES
        if overflow > 0:
            del self._diagnostics[:overflow]

    def _parse_authentication_records(self, chunk: bytes) -> None:
        self._pending_log_line.extend(chunk)
        if len(self._pending_log_line) > MAX_LOG_LINE_BYTES and b"\n" not in self._pending_log_line:
            self._pending_log_line.clear()
            self._lose_authentication_log_trust()
            return
        while b"\n" in self._pending_log_line:
            raw_line, _, remainder = self._pending_log_line.partition(b"\n")
            self._pending_log_line = bytearray(remainder)
            if len(raw_line) > MAX_LOG_LINE_BYTES:
                continue
            match = _AUTHENTICATED_RECORD.fullmatch(raw_line)
            if match is None:
                continue
            try:
                process_id = int(match.group(1))
                method = match.group(2).decode("ascii")
            except (UnicodeError, ValueError):
                continue
            self._auth_records.add((process_id, method))

    def _maybe_finalize_authentication(self) -> None:
        if (
            self._authentication_finalized
            or self._log_trust_lost
            or self._persistence_proof_disabled
        ):
            return
        expected_pid = self._expected_ssh_pid
        if expected_pid is None:
            return
        matching_methods = {
            method for process_id, method in self._auth_records if process_id == expected_pid
        }
        if len(matching_methods) != 1:
            return
        if not self._control_socket_is_trusted(expected_pid):
            if self._control_master_check_attempts >= MAX_CONTROL_MASTER_CHECKS:
                self._disable_password_persistence_proof(
                    "Signed in, but Termana could not verify the session for password saving.",
                    close_broker=True,
                )
            return
        self._expected_ssh_pid = expected_pid
        self._authentication_finalized = True
        method = matching_methods.pop()
        # Keep collecting -E diagnostics, but never parse authentication proof again.
        self._log_timer.setInterval(POST_AUTH_LOG_POLL_INTERVAL_MS)
        self._discard_post_auth_log_line = self._auth_log_offset > len(self._diagnostics)
        channel_failed = self._parse_post_authentication_diagnostics(bytes(self._diagnostics))
        self._revoke_control_socket()
        self._close_broker()
        if method == "password" and self._password_change_seen:
            self._handle_successful_password_change()
        elif method == "password" and self._saved_password_used:
            self.status_message.emit("Signed in with the saved SSH password.")
        elif (
            method == "password"
            and self._candidate_password is not None
            and self._candidate_username is not None
            and not self._password_change_seen
        ):
            self._offer_password_persistence()
        self._clear_password_candidate()
        if channel_failed and not self._closed:
            self._report_channel_open_failure()

    def _handle_successful_password_change(self) -> None:
        """Discard an obsolete keychain entry after OpenSSH proves a password change."""
        credential_id = self._credential_id
        credential_generation = self._credential_generation
        if credential_id is None or credential_generation is None:
            self.status_message.emit("The SSH password changed; the new password was not saved.")
            return
        operation_id = uuid4()
        pending = _PendingPostAuthCredentialOperation(
            operation_id=operation_id,
            kind=_PostAuthCredentialOperationKind.REMOVE_OUTDATED_PASSWORD,
        )
        self._post_auth_credential_operations[operation_id] = pending
        try:
            self._credential_mutation_coordinator.start_outdated_password_removal(
                operation_id,
                self._repository,
                credential_id,
                credential_generation,
            )
        except Exception:
            if pending.completed:
                return
            self._post_auth_credential_operations.pop(operation_id, None)
            pending.completed = True
            self._emit_post_auth_credential_operation_failure(pending)

    def _offer_password_persistence(self) -> None:
        password = self._candidate_password
        username = self._candidate_username
        if password is None or username is None:
            return
        self._clear_password_candidate()
        try:
            should_save = self._request_confirmation(
                self._save_confirmation_prompt,
                endpoint=_display_endpoint(self._endpoint),
                username=username,
                parent=self._dialog_parent,
            )
            if self._closed:
                return
            if not should_save:
                self.status_message.emit("SSH password was not saved.")
                return
            operation_id = uuid4()
            pending = _PendingPostAuthCredentialOperation(
                operation_id=operation_id,
                kind=_PostAuthCredentialOperationKind.SAVE_PASSWORD,
                username=username,
            )
            self._post_auth_credential_operations[operation_id] = pending
            try:
                self._credential_mutation_coordinator.start_password_save(
                    operation_id,
                    self._repository,
                    self._endpoint,
                    username,
                    password,
                )
            except Exception:
                if pending.completed:
                    return
                self._post_auth_credential_operations.pop(operation_id, None)
                pending.completed = True
                self._emit_post_auth_credential_operation_failure(pending)
        finally:
            password = None
            username = None

    def _request_confirmation(self, provider: ConfirmationPrompt, **values: object) -> bool:
        """Keep built-in modal consent owned by this exact live SSH session."""
        if self._closed or self._confirmation_dialog is not None:
            return False
        if provider is not confirm_ssh_action and provider is not confirm_save_ssh_password:
            # Injected confirmation providers retain their existing call contract.
            return provider(**values)

        def register(dialog: QDialog) -> bool:
            if self._closed:
                return False
            self._confirmation_dialog = dialog
            return True

        try:
            return provider(**values, dialog_created=register)
        finally:
            self._confirmation_dialog = None

    @Slot(object, object)
    def _handle_post_auth_credential_operation_finished(
        self,
        operation_id: object,
        outcome: object,
    ) -> None:
        """Settle one detached transaction only for its exact live auth owner."""
        pending = self._take_post_auth_credential_operation(operation_id)
        if pending is None:
            return
        pending.completed = True
        if self._closed:
            return
        if pending.kind is _PostAuthCredentialOperationKind.SAVE_PASSWORD:
            if outcome is not SshCredentialMutationOutcome.SAVE_SUCCEEDED:
                self._emit_post_auth_credential_operation_failure(pending)
                return
            self.status_message.emit(
                f'SSH password for "{pending.username}" was saved securely. '
                "Choose this login on the next connection."
            )
            return
        if pending.kind is _PostAuthCredentialOperationKind.REMOVE_OUTDATED_PASSWORD:
            if outcome is SshCredentialMutationOutcome.REMOVE_SUCCEEDED:
                self.status_message.emit(
                    "The SSH password changed; the outdated saved password was removed."
                )
            elif outcome is SshCredentialMutationOutcome.REMOVE_MISSING:
                self.status_message.emit(
                    "The SSH password changed; there was no saved password to update."
                )
            elif outcome is SshCredentialMutationOutcome.REMOVE_GENERATION_CHANGED:
                self.status_message.emit(
                    "The SSH password changed; a newer saved password was kept."
                )
            else:
                self._emit_post_auth_credential_operation_failure(pending)

    def _take_post_auth_credential_operation(
        self,
        operation_id: object,
    ) -> _PendingPostAuthCredentialOperation | None:
        """Consume exactly one retained transaction by its opaque identity."""
        if not isinstance(operation_id, UUID):
            return None
        return self._post_auth_credential_operations.pop(operation_id, None)

    def _emit_post_auth_credential_operation_failure(
        self,
        pending: _PendingPostAuthCredentialOperation,
    ) -> None:
        """Report a mutation failure only while this session still owns live UI."""
        if self._closed:
            return
        if pending.kind is _PostAuthCredentialOperationKind.SAVE_PASSWORD:
            self.status_message.emit(
                "Signed in, but the password could not be saved in the system credential store."
            )
            return
        self.status_message.emit(
            "The SSH password changed, but the outdated saved password could not be removed."
        )

    def _remember_password_candidate(self, password: str, username: str) -> None:
        """Retain one manual password only for a short, proof-bearing save window."""
        if self._persistence_proof_disabled:
            return
        self._candidate_password = password
        self._candidate_username = username
        self._candidate_expires_at = time.monotonic() + PASSWORD_PERSISTENCE_PROOF_TIMEOUT_SECONDS
        self._saved_password_used = False

    def _clear_password_candidate(self) -> None:
        """Release every application-held reference to an unsaved password candidate."""
        self._candidate_password = None
        self._candidate_username = None
        self._candidate_expires_at = 0.0

    def _expire_password_candidate(self) -> None:
        """Drop a password when successful authentication was not proved promptly."""
        if (
            self._candidate_password is None
            or self._candidate_expires_at <= 0
            or time.monotonic() < self._candidate_expires_at
        ):
            return
        self._clear_password_candidate()
        self.status_message.emit(
            "The SSH password was not retained for saving because sign-in was not confirmed in time."
        )

    def _lose_authentication_log_trust(self) -> None:
        """Disable persistence immediately when the local proof log becomes unsafe."""
        self._log_trust_lost = True
        self._auth_records.clear()
        self._disable_password_persistence_proof(
            "SSH password saving was disabled because the session proof became unavailable."
        )

    def _disable_password_persistence_proof(
        self,
        message: str,
        *,
        close_broker: bool = False,
    ) -> None:
        """Fail closed once this session can no longer prove a password login."""
        if self._persistence_proof_disabled:
            return
        self._persistence_proof_disabled = True
        self._clear_password_candidate()
        self.status_message.emit(message)
        if close_broker:
            self._log_timer.stop()
            self._close_broker()

    def _revoke_control_socket(self) -> None:
        """Unlink the verified mux listener while leaving the live SSH session intact."""
        try:
            metadata = os.lstat(self._control_socket_path)
            if (
                stat.S_ISSOCK(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and metadata.st_nlink == 1
            ):
                os.unlink(self._control_socket_path)
        except OSError:
            pass

    def _control_socket_is_trusted(self, expected_pid: int) -> bool:
        try:
            directory = os.lstat(self._runtime_directory)
            control_socket = os.lstat(self._control_socket_path)
        except OSError:
            return False
        metadata_is_trusted = (
            stat.S_ISDIR(directory.st_mode)
            and directory.st_uid == os.getuid()
            and stat.S_IMODE(directory.st_mode) == 0o700
            and stat.S_ISSOCK(control_socket.st_mode)
            and control_socket.st_uid == os.getuid()
            and control_socket.st_nlink == 1
            and stat.S_IMODE(control_socket.st_mode) & 0o077 == 0
        )
        if not metadata_is_trusted:
            return False

        now = time.monotonic()
        if self._next_control_master_check_at == 0:
            self._next_control_master_check_at = now + CONTROL_MASTER_CHECK_DELAY_SECONDS
            return False
        if (
            now < self._next_control_master_check_at
            or self._control_master_check_attempts >= MAX_CONTROL_MASTER_CHECKS
        ):
            return False
        self._control_master_check_attempts += 1
        if self._control_master_check(self._control_socket_path, expected_pid):
            return True
        self._next_control_master_check_at = now + (
            CONTROL_MASTER_CHECK_DELAY_SECONDS * (2**self._control_master_check_attempts)
        )
        return False

    def _check_control_master(self, control_path: str, expected_pid: int) -> bool:
        """Authenticate the mux listener directly through kernel peer credentials."""
        control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            control_socket.settimeout(CONTROL_SOCKET_CONNECT_TIMEOUT_SECONDS)
            control_socket.connect(control_path)
            peer = _unix_peer_credentials(control_socket)
        except OSError:
            return False
        finally:
            control_socket.close()
        return peer == (os.getuid(), expected_pid)

    def _close_broker(self) -> None:
        if self._server.isListening():
            self._server.close()
        clients = tuple(
            dict.fromkeys(
                (
                    *self._client_buffers,
                    *self._client_peers,
                    *(
                        (self._pending_secret_request.client,)
                        if self._pending_secret_request is not None
                        else ()
                    ),
                )
            )
        )
        for client in clients:
            client.abort()
            self._drop_client(client)
        if self._askpass_socket_path:
            QLocalServer.removeServer(self._askpass_socket_path)

    def _cleanup_paths(self) -> None:
        for path in (
            self._askpass_socket_path,
            self._control_socket_path,
            self._auth_log_path,
        ):
            if not path or os.path.dirname(path) != self._runtime_directory:
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if self._runtime_directory:
            try:
                os.rmdir(self._runtime_directory)
            except OSError:
                pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate AskPass request field")
        result[key] = value
    return result


def _bounded_text(value: object, maximum_bytes: int) -> bool:
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum_bytes
    except UnicodeError:
        return False
