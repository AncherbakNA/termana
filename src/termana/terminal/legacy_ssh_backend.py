"""Owned PTY/ConPTY terminal and authenticated Plink frontend control channel.

Only verified frontend callbacks can request secrets or report authentication.
Remote terminal bytes cannot trigger vault reads, trust writes or a save offer.
"""

import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtCore import QThreadPool, QTimer, Signal, Slot
from PySide6.QtNetwork import QHostAddress, QTcpServer

from termana.credentials.background_operation import BackgroundOperation
from termana.credentials.embedded_ssh_auth import ProvenSshPassword
from termana.credentials.legacy_ssh_trust import review_legacy_key, validate_legacy_key
from termana.credentials.secret_store import MAX_SSH_PASSWORD_CHARACTERS
from termana.credentials.ssh_auth import SshSecretRequest
from termana.models.connection import ConnectionProfile, ConnectionProtocol
from termana.models.ssh_credential import SshEndpoint, validate_ssh_username
from termana.models.ssh_identity import SshAuthenticationMode
from termana.models.ssh_jump import SshJumpMode, validate_ssh_route_host
from termana.terminal.backend import TerminalBackend
from termana.terminal.backend_factory import create_terminal_backend
from termana.terminal.command import ShellCommand
from termana.terminal.legacy_ssh import LegacySshError, verify_legacy_frontend

_HEADER = struct.Struct("!cI")
_LIMIT = 32768
MAX_LEGACY_SECRET_BYTES = 16384


@dataclass(frozen=True)
class LegacyConnectionPlan:
    profile: ConnectionProfile
    known_hosts: Path
    helper: Path

    def __post_init__(self):
        validate_legacy_profile(self.profile)
        if self.profile.username is None:
            raise LegacySshError("SSH1 needs an explicit login.")
        validate_ssh_username(self.profile.username)
        if not isinstance(self.known_hosts, Path) or not self.known_hosts.is_absolute():
            raise LegacySshError("SSH1 requires Termana's separate trust file.")

    @property
    def endpoint(self):
        return SshEndpoint(self.profile.host, self.profile.port)


def validate_legacy_profile(profile):
    if (type(profile) is not ConnectionProfile or profile.protocol is not ConnectionProtocol.SSH
            or profile.ssh_algorithms.protocol != "1"):
        raise LegacySshError("Choose SSH1 explicitly; automatic downgrade is forbidden.")
    validate_ssh_route_host(profile.host)
    if profile.ssh_jump.mode is not SshJumpMode.DIRECT:
        raise LegacySshError("SSH1 requires an explicit Direct route. Gateway/config routes are not supported.")
    if (not profile.ssh_tunnels.is_default or profile.ssh_cipher is not None
            or profile.ssh_algorithms.kex or profile.ssh_algorithms.host_keys
            or not profile.ssh_options.is_default):
        raise LegacySshError("SSH1 does not support SSH2 algorithm overrides, tunnels or custom keepalives/timeouts. Reset those options first.")
    identity = profile.ssh_identity
    if identity.identities_only is False:
        raise LegacySshError("SSH1 supports only a selected RSA1 key and passwords, not inherited identities. Use Default or Selected keys only.")
    if identity.authentication is SshAuthenticationMode.KEY_ONLY and identity.identity_file is None:
        raise LegacySshError("SSH1 key-only authentication requires a selected RSA1 private key.")


class LegacySshBackend(TerminalBackend):
    secret_requested = Signal(object)
    secret_request_finished = Signal(object)
    authenticated = Signal()

    def __init__(self, plan, authentication_id, *, insecure_protocol_approved=False,
                 parent=None, backend_factory=create_terminal_backend):
        super().__init__(parent)
        if (type(plan) is not LegacyConnectionPlan or type(authentication_id) is not UUID
                or insecure_protocol_approved is not True):
            raise LegacySshError("SSH1 needs explicit insecure-protocol consent every launch.")
        self.plan, self.authentication_id = plan, authentication_id
        self._factory = backend_factory
        self._backend = None
        self._server = QTcpServer(self)
        self._server.setMaxPendingConnections(2)
        self._server.newConnection.connect(self._accept)
        self._client = None
        self._buffer = bytearray()
        self._token = secrets.token_hex(32)
        self._hello = self._trusted = self._ready = False
        self._control_lost = False
        self._session_started = False
        self._started = self._closed = self._completed = False
        self._request = self._review = self._operation = None
        self._candidate = self._proof = None
        self._proof_deadline = 0.0
        self._attempts = self._connections = 0
        self._size = (80, 24)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._fail)
        self._proof_timer = QTimer(self)
        self._proof_timer.setSingleShot(True)
        self._proof_timer.timeout.connect(self._clear_proof)
        self._login_timer = QTimer(self)
        self._login_timer.setSingleShot(True)
        self._login_timer.timeout.connect(self._fail)

    @property
    def is_running(self):
        return self._started and not self._completed

    @property
    def text_encoding(self):
        return "utf-8"

    @property
    def echoes_input(self):
        return True

    @property
    def supports_direct_input(self):
        return True

    @property
    def process_id(self):
        return None if self._backend is None else self._backend.process_id

    @property
    def current_secret_request(self):
        return None if self._closed else self._request

    @property
    def authentication_live(self):
        return self._ready and not self._closed and self.is_running

    @property
    def password_proof_deadline(self):
        return self._proof_deadline

    def take_proven_password(self):
        proof, self._proof = self._proof, None
        return proof if self.authentication_live and time.monotonic() < self._proof_deadline else None

    @property
    def sftp_available(self):
        return False

    def start(self):
        if self._started or self._closed:
            return
        self._started = True
        # Hash/filesystem operations run outside the GUI. No process, trust or
        # vault access on an unsupported/missing/modified helper.
        self._run_operation(self._prepare_command, self._launch)
        self._timeout.start(15000)
        self._login_timer.start(300000)

    def _prepare_command(self):
        helper = verify_legacy_frontend(self.plan.helper)
        identity = self.plan.profile.ssh_identity
        if (identity.identity_file is not None
                and identity.authentication is not SshAuthenticationMode.PASSWORD_ONLY):
            from termana.credentials.embedded_ssh_keys import read_embedded_private_key

            data = read_embedded_private_key(Path(identity.identity_file))
            if not data.startswith(b"SSH PRIVATE KEY FILE FORMAT 1.1\n"):
                raise LegacySshError("Select a legacy RSA1 private key.")
            del data
        return helper

    @Slot(object, object)
    def _operation_done(self, operation_id, result):
        pending = self._operation
        if pending is None or pending[0] != operation_id:
            return
        self._operation = None
        if not self._closed and not self._control_lost:
            pending[2](result)

    def _run_operation(self, action, completion):
        if self._operation is not None:
            self._fail()
            return
        operation_id = uuid4()
        task = BackgroundOperation(operation_id, action)
        self._operation = (operation_id, task, completion)
        task.signals.succeeded.connect(self._operation_done)
        task.signals.failed.connect(self._operation_failed)
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _operation_failed(self, operation_id):
        if self._operation is not None and operation_id == self._operation[0]:
            self._operation = None
            self._fail()

    def _launch(self, helper):
        if not self._server.listen(QHostAddress.SpecialAddress.LocalHost, 0):
            self._fail()
            return
        profile = self.plan.profile
        args = ["-ssh", "-1", "-t", "-noagent", "-a", "-x", "-noshare", "-no-antispoof",
                "-P", str(profile.port), "-l", profile.username]
        identity = profile.ssh_identity
        if identity.identity_file and identity.authentication is not SshAuthenticationMode.PASSWORD_ONLY:
            args.extend(("-i", identity.identity_file))
        args.append(profile.host)
        command = ShellCommand(helper, tuple(args), environment=(
            ("TERMANA_SSH1_CONTROL_PORT", str(self._server.serverPort())),
            ("TERMANA_SSH1_CONTROL_TOKEN", self._token),
        ))
        self._backend = self._factory(command)
        self._backend.setParent(self)
        self._backend.resize(*self._size)
        self._backend.data_received.connect(self.data_received)
        self._backend.finished.connect(self._finished)
        self._backend.error_occurred.connect(self._transport_error)
        self._backend.warning_occurred.connect(self.warning_occurred)
        self._backend.start()

    def _accept(self):
        while self._server.hasPendingConnections():
            client = self._server.nextPendingConnection()
            self._connections += 1
            if self._closed or self._client is not None or self._connections > 4:
                client.abort()
                client.deleteLater()
                continue
            self._client = client
            client.setReadBufferSize(_LIMIT + _HEADER.size)
            client.readyRead.connect(self._read)
            client.disconnected.connect(self._disconnected)
            self._read()

    def _read(self):
        if self._closed or self._control_lost or self._client is None:
            return
        self._buffer.extend(bytes(self._client.readAll()))
        if len(self._buffer) > _LIMIT + _HEADER.size:
            self._fail()
            return
        if len(self._buffer) < _HEADER.size:
            return
        if not self._hello and self.process_id is None:
            # On ConPTY the child can connect before its PID control frame has
            # reached Qt. Keep, never accept, the hello until owned PID arrives.
            QTimer.singleShot(10, self._read)
            return
        kind, length = _HEADER.unpack_from(self._buffer)
        if length > _LIMIT:
            self._fail()
            return
        if len(self._buffer) < _HEADER.size + length:
            return
        data = bytes(self._buffer[_HEADER.size:])
        self._buffer.clear()
        if len(data) != length or self._request is not None or self._operation is not None:
            self._fail()
            return
        try:
            self._handle(kind, data)
        except (ValueError, TypeError, UnicodeError):
            self._fail()

    def _handle(self, kind, data):
        if not self._hello:
            expected = f"1:{self.process_id}:{self._token}".encode("ascii")
            if kind != b"H" or not hmac.compare_digest(data, expected):
                raise ValueError
            self._hello = True
            self._token = ""
            self._server.close()
            self._answer(b"OK")
        elif kind == b"K" and not self._trusted and self._review is None:
            host, port, keytype, key, fingerprint = data.decode("ascii").split("\0")
            if (host != self.plan.profile.host or port != str(self.plan.profile.port)
                    or keytype != "rsa"):
                raise ValueError
            validate_legacy_key(key, fingerprint)
            self._run_operation(lambda: review_legacy_key(
                self.plan.known_hosts, self.plan.endpoint, key, fingerprint), self._reviewed)
        elif kind == b"P" and self._trusted and not self._ready:
            password = data == b"password"
            if data not in (b"password", b"passphrase") or self._attempts >= 6:
                raise ValueError
            if password and self.plan.profile.ssh_identity.authentication is SshAuthenticationMode.KEY_ONLY:
                raise ValueError
            self._attempts += 1
            self._candidate = None
            self._request = SshSecretRequest(
                uuid4(), f"{self.plan.endpoint.host}:{self.plan.endpoint.port}",
                "Password:" if password else "RSA1 private key passphrase:",
                self.plan.profile.username, password)
            self._timeout.start(60000)
            self.secret_requested.emit(self._request)
        elif kind == b"R" and not data and self._trusted and not self._ready:
            self._ready = True
            self._session_started = True
            self._timeout.stop()
            self._login_timer.stop()
            if self._candidate is not None:
                self._proof_deadline = time.monotonic() + 30
                self._proof = ProvenSshPassword(self.authentication_id, self.plan.endpoint,
                                               self.plan.profile.username, self._candidate)
                self._proof_timer.start(30000)
            self._candidate = None
            self._answer(b"OK")
            self.started.emit()
            self.authenticated.emit()
        else:
            raise ValueError

    def _reviewed(self, review):
        self._review = review
        if review.trusted:
            self._trust_ready(None)
            return
        self._request = SshSecretRequest(uuid4(), f"{self.plan.endpoint.host}:{self.plan.endpoint.port}",
                                        review.prompt, self.plan.profile.username, False, True)
        self._timeout.start(60000)
        self.secret_requested.emit(self._request)

    def _trust_ready(self, _result):
        self._trusted = True
        self._timeout.start(60000)
        self._answer(b"OK")

    def submit_secret(self, request_id, value):
        request = self.current_secret_request
        if (request is None or request.request_id != request_id or type(value) is not str
                or not value or len(value) > MAX_SSH_PASSWORD_CHARACTERS
                or any(c in value for c in "\0\r\n")):
            return False
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            return False
        if len(encoded) > MAX_LEGACY_SECRET_BYTES:
            return False
        self._request = None
        self.secret_request_finished.emit(request)
        if self._closed:
            return False
        if request.is_host_confirmation:
            if value.strip().lower() != "yes":
                self.close()
            else:
                self._run_operation(self._review.commit, self._trust_ready)
        else:
            if request.is_host_password:
                self._candidate = value
            self._answer(encoded)
            self._timeout.start(15000)
        return True

    def _answer(self, data):
        if self._closed or self._control_lost or self._client is None:
            return
        self._client.write(_HEADER.pack(b"A", len(data)) + data)
        self._client.flush()

    def _disconnected(self):
        # The socket is retained for the whole process lifetime. Parent closure
        # or unexpected helper loss must revoke requests/proof immediately.
        if not self._closed:
            self._control_lost = True
            self._ready = False
            self._clear_proof()
            if self._request is not None:
                request, self._request = self._request, None
                self.secret_request_finished.emit(request)
            # EOF also occurs at normal process exit. Let the PTY collect the
            # exact exit code and final output before terminating an orphan.
            if not self._session_started:
                self._fail()
            else:
                self._timeout.start(5000)

    def write(self, data):
        if self.authentication_live:
            self._backend.write(data)

    def resize(self, columns, rows):
        if type(columns) is not int or type(rows) is not int or not (1 <= columns <= 32767 and 1 <= rows <= 32767):
            raise ValueError("Invalid SSH1 terminal dimensions")
        self._size = columns, rows
        if self._backend is not None:
            self._backend.resize(columns, rows)

    def _clear_proof(self):
        self._proof = self._candidate = None

    def _transport_error(self, _message):
        self._fail()

    def _fail(self):
        if not self._closed:
            self.error_occurred.emit("SSH1 stopped: helper, trust, authentication or control-channel verification failed. No fallback was attempted.")
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._timeout.stop()
        self._login_timer.stop()
        self._proof_timer.stop()
        self._clear_proof()
        self._token = ""
        request, self._request = self._request, None
        if request is not None:
            self.secret_request_finished.emit(request)
        self._server.close()
        if self._client is not None:
            self._client.abort()
        if self._backend is not None:
            self._backend.close()
        else:
            QTimer.singleShot(0, lambda: self._finished(255))

    def _finished(self, code):
        if self._completed:
            return
        if not self._session_started and not self._closed:
            self.error_occurred.emit("SSH1 ended before verified authentication completed.")
            code = 255
        self.close()
        self._completed = True
        self.finished.emit(code)
