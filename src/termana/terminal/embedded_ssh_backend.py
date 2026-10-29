"""Bounded Qt/asyncio bridge for the explicitly selected embedded SSH engine.

The worker never calls Qt. Passwords and proof live in a locked, one-shot
mailbox, not queued signals, subprocess arguments, environment or diagnostics.
Only the GUI thread publishes metadata and bounded terminal output.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from threading import Event, Lock, Thread
from typing import Final
from uuid import UUID, uuid4

from PySide6.QtCore import QThread, QTimer, Signal, Slot

from termana.credentials.embedded_ssh_auth import (
    MAX_EMBEDDED_PASSWORD_BYTES,
    ProvenSshPassword,
)
from termana.credentials.ssh_auth import SshSecretRequest
from termana.models.sftp import MAX_SFTP_PENDING, SftpProgress, SftpRequest, SftpResult
from termana.models.ssh_negotiation import SshNegotiatedFacts
from termana.terminal.backend import MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN, TerminalBackend
from termana.terminal.embedded_ssh import (
    EMBEDDED_LOGIN_TIMEOUT_SECONDS,
    EmbeddedSshError,
    EmbeddedSshFailure,
    EmbeddedSshPlan,
    connect_embedded_ssh,
    validate_embedded_terminal_size,
)

EMBEDDED_UI_POLL_MS: Final = 10
EMBEDDED_IDLE_POLL_MS: Final = 80
EMBEDDED_ACTIVE_HOLD_SECONDS: Final = 0.15
MAX_EMBEDDED_PENDING_BYTES: Final = 64 * 1024
EMBEDDED_GUI_PROOF_TIMEOUT_SECONDS: Final = 30
EMBEDDED_HOST_CONFIRM_TIMEOUT_SECONDS: Final = 60
_NO_ANSWER = object()


class EmbeddedSshBackend(TerminalBackend):
    """Own one worker until all its SSH resources are closed and it has exited."""

    secret_requested = Signal(object)
    secret_request_finished = Signal(object)
    authenticated = Signal()
    gateway_authenticated = Signal()
    negotiation_changed = Signal()
    sftp_result = Signal(object)
    sftp_progress = Signal(object)

    def __init__(
        self,
        plan: EmbeddedSshPlan,
        authentication_id: UUID,
        parent=None,
        *,
        interactive_host_trust: bool = False,
    ) -> None:
        super().__init__(parent)
        if type(plan) is not EmbeddedSshPlan or type(authentication_id) is not UUID:
            raise TypeError("Embedded SSH requires a validated plan and authentication UUID")
        self._plan = plan
        if type(interactive_host_trust) is not bool:
            raise TypeError("interactive host trust must be a boolean")
        self._interactive_host_trust = interactive_host_trust
        self._trust_event = Event()
        self._trust_waiting = False
        self._trust_fingerprint = ""
        self._login_deadline = 0.0
        self._trust_deadline = 0.0
        self._authentication_id = authentication_id
        self._gateway_authentication_id = uuid4()
        self._gateway_live = False
        self._gateway_proof = None
        self._gateway_proof_deadline = 0.0
        self._gateway_published = False
        self._lock = Lock()
        self._thread: Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task | None = None
        self._input_event: asyncio.Event | None = None
        self._output_space: asyncio.Event | None = None
        self._secret_event: asyncio.Event | None = None
        self._input: deque[bytes] = deque()
        self._input_bytes = 0
        self._output = bytearray()
        self._size = (80, 24)
        self._resize_pending = False
        self._stopping = False
        self._connected = False
        self._ready = False
        self._negotiated_facts: SshNegotiatedFacts | None = None
        self._published_facts: SshNegotiatedFacts | None = None
        self._sftp_runtime = None
        self._sftp_pending = set()
        self._sftp_results = deque()
        self._sftp_progress = {}
        self._worker_done = False
        self._exit_code = 255
        self._failure: EmbeddedSshFailure | None = None
        self._request: SshSecretRequest | None = None
        self._answer: object = _NO_ANSWER
        self._proof: ProvenSshPassword | None = None
        self._proof_deadline = 0.0
        self._start_attempted = False
        self._completion_emitted = False
        self._published_request: SshSecretRequest | None = None
        self._started_emitted = False
        self._active_until = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(EMBEDDED_UI_POLL_MS)
        self._timer.timeout.connect(self._poll)

    @property
    def plan(self) -> EmbeddedSshPlan:
        return self._plan

    @property
    def authentication_id(self) -> UUID:
        return self._authentication_id

    @property
    def gateway_authentication_id(self) -> UUID:
        return self._gateway_authentication_id

    @property
    def gateway_authentication_live(self) -> bool:
        with self._lock:
            return self._gateway_live and not self._stopping and not self._worker_done

    @property
    def gateway_password_proof_deadline(self) -> float:
        with self._lock:
            return self._gateway_proof_deadline

    def take_gateway_proven_password(self):
        with self._lock:
            proof, self._gateway_proof = self._gateway_proof, None
            if (
                not self._gateway_live
                or self._stopping
                or self._worker_done
                or time.monotonic() >= self._gateway_proof_deadline
            ):
                return None
            return proof

    @property
    def current_secret_request(self) -> SshSecretRequest | None:
        with self._lock:
            return None if self._stopping or self._worker_done else self._request

    @property
    def password_proof_deadline(self) -> float:
        with self._lock:
            return self._proof_deadline

    @property
    def is_running(self) -> bool:
        return self._start_attempted and not self._completion_emitted

    @property
    def authentication_live(self) -> bool:
        with self._lock:
            return self._connected and not self._stopping and not self._worker_done

    @property
    def negotiated_facts(self) -> SshNegotiatedFacts | None:
        """Only this backend's immutable live-owner snapshot crosses to the GUI."""
        with self._lock:
            return (
                self._negotiated_facts
                if self._ready and self._connected and not self._stopping and not self._worker_done
                else None
            )

    @property
    def text_encoding(self) -> str:
        return "utf-8"

    @property
    def echoes_input(self) -> bool:
        return True

    @property
    def supports_direct_input(self) -> bool:
        return True

    def start(self) -> None:
        if self._start_attempted or self._stopping:
            return
        self._start_attempted = True
        self._thread = Thread(target=self._run_worker, name="Termana-embedded-SSH", daemon=False)
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None
            self._worker_done = True
            self._failure = EmbeddedSshFailure.CONNECTION
        self._timer.start()

    def write(self, data: bytes) -> None:
        if type(data) is not bytes:
            raise TypeError("Embedded SSH input must be bytes")
        warning = None
        with self._lock:
            if self._stopping or not self._ready or self._worker_done:
                warning = "The SSH connection is not ready for input."
            elif self._input_bytes + len(data) > MAX_EMBEDDED_PENDING_BYTES:
                warning = "SSH input is busy; this input was not sent."
            else:
                for offset in range(0, len(data), MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN):
                    self._input.append(data[offset : offset + MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN])
                self._input_bytes += len(data)
                self._wake_locked(self._input_event)
        if warning is not None:
            self.warning_occurred.emit(warning)
        else:
            self._mark_gui_activity()

    def _mark_gui_activity(self):
        # Only the GUI caller changes Qt timers. Worker I/O stays Qt-free.
        if QThread.currentThread() == self.thread():
            self._active_until = time.monotonic() + EMBEDDED_ACTIVE_HOLD_SECONDS
            self._timer.setInterval(EMBEDDED_UI_POLL_MS)

    def resize(self, columns: int, rows: int) -> None:
        validate_embedded_terminal_size(columns, rows)
        with self._lock:
            if self._stopping or self._worker_done:
                return
            self._size = (columns, rows)
            self._resize_pending = True
            self._wake_locked(self._input_event)

    def submit_secret(self, request_id: UUID, value: str) -> bool:
        # Use the same bounds as the transport, before retaining any value.
        try:
            valid = (
                type(value) is str
                and "\0" not in value
                and len(value) <= MAX_EMBEDDED_PASSWORD_BYTES // 4
                and len(value.encode("utf-8")) <= MAX_EMBEDDED_PASSWORD_BYTES
            )
        except UnicodeError:
            valid = False
        with self._lock:
            if (
                not valid
                or self._stopping
                or self._worker_done
                or self._request is None
                or self._request.request_id != request_id
                or self._answer is not _NO_ANSWER
                or (self._trust_waiting and value not in {"yes", "no", self._trust_fingerprint})
                or (self._trust_waiting and time.monotonic() >= self._trust_deadline)
            ):
                return False
            self._answer = (
                "yes" if self._trust_waiting and value == self._trust_fingerprint else value
            )
            if self._trust_waiting:
                self._trust_event.set()
            self._wake_locked(self._secret_event)
        return True

    def take_proven_password(self) -> ProvenSshPassword | None:
        with self._lock:
            proof = self._proof
            self._proof = None
            if (
                self._stopping
                or self._worker_done
                or not self._connected
                or time.monotonic() >= self._proof_deadline
            ):
                return None
            return proof

    @property
    def sftp_available(self) -> bool:
        with self._lock:
            return self._ready and self._connected and not self._stopping and not self._worker_done

    def request_sftp(self, request: SftpRequest) -> bool:
        if type(request) is not SftpRequest:
            return False
        with self._lock:
            if (
                not self._ready
                or not self._connected
                or self._stopping
                or self._worker_done
                or self._sftp_runtime is None
                or self._loop is None
                or request.request_id in self._sftp_pending
                or len(self._sftp_pending) >= MAX_SFTP_PENDING
            ):
                return False
            self._sftp_pending.add(request.request_id)
            try:
                self._loop.call_soon_threadsafe(self._sftp_runtime.enqueue, request)
            except RuntimeError:
                self._sftp_pending.discard(request.request_id)
                return False
        return True

    def cancel_sftp(self, request_id: UUID) -> bool:
        with self._lock:
            if (
                request_id not in self._sftp_pending
                or self._sftp_runtime is None
                or self._loop is None
                or self._worker_done
            ):
                return False
            try:
                self._loop.call_soon_threadsafe(self._sftp_runtime.cancel, request_id)
            except RuntimeError:
                return False
        return True

    def _publish_sftp(self, event):
        with self._lock:
            if event.request_id not in self._sftp_pending:
                return
            if isinstance(event, SftpProgress):
                self._sftp_progress[event.request_id] = event
            else:
                self._sftp_results.append(event)

    def close(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._negotiated_facts = None
            self._gateway_proof = None
            self._gateway_live = False
            self._trust_event.set()
            self._answer = _NO_ANSWER
            self._request = None
            self._proof = None
            self._input.clear()
            self._input_bytes = 0
            self._output.clear()
            if self._loop is not None and self._main_task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._main_task.cancel)
                except RuntimeError:
                    pass
        if not self._start_attempted:
            self._worker_done = True
            self._timer.start()

    def _wake_locked(self, event: asyncio.Event | None) -> None:
        if self._loop is not None and event is not None:
            try:
                # No confidential argument is captured in the queued callback.
                self._loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def _run_worker(self) -> None:
        try:
            asyncio.run(self._run_connection())
        except Exception:
            with self._lock:
                self._failure = EmbeddedSshFailure.CONNECTION
        finally:
            with self._lock:
                self._connected = False
                self._negotiated_facts = None
                self._worker_done = True
                self._loop = None
                self._main_task = None
                self._input_event = self._output_space = self._secret_event = None
                self._request = None
                self._answer = _NO_ANSWER
                self._proof = None
                self._gateway_proof = None
                self._gateway_live = False
                self._input.clear()
                self._input_bytes = 0
                self._sftp_runtime = None
                completed = {result.request_id for result in self._sftp_results}
                for request_id in self._sftp_pending - completed:
                    self._sftp_results.append(
                        SftpResult(request_id, "error", "SSH ended; check destination state")
                    )

    async def _password(self) -> str | None:
        return await self._secret("Password:", is_host_password=True)

    def _confirm_host_key(self, question, *, is_gateway=False) -> bool:
        """Bound only this worker, never the GUI or another SSH connection.

        asyncio cannot service cancellation/timers during a synchronous library
        callback. A thread Event wakes on close; a monotonic deadline is enforced
        here before returning consent, including an expired login deadline.
        """
        self._trust_deadline = min(
            time.monotonic() + EMBEDDED_HOST_CONFIRM_TIMEOUT_SECONDS, self._login_deadline
        )
        remaining = self._trust_deadline - time.monotonic()
        if remaining <= 0:
            return False
        request = SshSecretRequest(
            uuid4(),
            question.display_endpoint,
            question.prompt + f"\nAnswer within {max(1, int(remaining))} seconds.",
            self._plan.profile.ssh_jump.username if is_gateway else self._plan.profile.username,
            False,
            True,
            is_gateway,
        )
        with self._lock:
            if self._stopping:
                return False
            self._trust_event.clear()
            self._request = request
            self._answer = _NO_ANSWER
            self._trust_waiting = True
            self._trust_fingerprint = question.fingerprint
        try:
            self._trust_event.wait(max(0, self._trust_deadline - time.monotonic()))
            with self._lock:
                return (
                    not self._stopping
                    and time.monotonic() < self._trust_deadline
                    and self._request is request
                    and self._answer == "yes"
                )
        finally:
            with self._lock:
                self._trust_waiting = False
                self._trust_fingerprint = ""
                self._request = None
                self._answer = _NO_ANSWER

    async def _passphrase(self) -> str | None:
        return await self._secret("Private key passphrase:", is_host_password=False)

    async def _gateway_password(self) -> str | None:
        from termana.models.ssh_credential import SshEndpoint

        route = self._plan.profile.ssh_jump
        endpoint = SshEndpoint(route.host, route.port or 22)
        return await self._secret(
            f"Gateway {route.username}@{route.host}: Password:",
            is_host_password=False,
            endpoint_identity=endpoint,
            username=route.username,
            is_gateway=True,
        )

    async def _secret(
        self,
        prompt: str,
        *,
        is_host_password: bool,
        endpoint_identity=None,
        username=None,
        is_gateway=False,
    ) -> str | None:
        identity = endpoint_identity or self._plan.endpoint
        host = identity.host
        endpoint = f"[{host}]:{identity.port}" if ":" in host else f"{host}:{identity.port}"
        request = SshSecretRequest(
            uuid4(),
            endpoint,
            prompt,
            username or self._plan.profile.username,
            is_host_password,
            False,
            is_gateway,
        )
        with self._lock:
            if self._stopping:
                return None
            self._request = request
            self._answer = _NO_ANSWER
        try:
            while True:
                self._secret_event.clear()
                with self._lock:
                    if self._stopping:
                        return None
                    if self._answer is not _NO_ANSWER:
                        answer = self._answer
                        self._answer = _NO_ANSWER
                        return answer
                await self._secret_event.wait()
        finally:
            with self._lock:
                self._request = None
                self._answer = _NO_ANSWER

    async def _run_connection(self) -> None:
        owned = None
        runtime = None
        children = []
        try:
            with self._lock:
                if self._stopping:
                    return
                self._loop = asyncio.get_running_loop()
                self._login_deadline = time.monotonic() + EMBEDDED_LOGIN_TIMEOUT_SECONDS
                self._main_task = asyncio.current_task()
                self._input_event, self._output_space, self._secret_event = (
                    asyncio.Event(),
                    asyncio.Event(),
                    asyncio.Event(),
                )
            owned = await connect_embedded_ssh(
                self._plan,
                self._authentication_id,
                self._password,
                passphrase_provider=self._passphrase,
                disconnect_callback=self._authentication_ended,
                host_key_provider=(
                    self._confirm_host_key if self._interactive_host_trust else None
                ),
                gateway_password_provider=self._gateway_password,
                gateway_host_key_provider=(
                    lambda question: self._confirm_host_key(question, is_gateway=True)
                )
                if self._interactive_host_trust
                else None,
                gateway_authentication_id=self._gateway_authentication_id,
                gateway_ready_callback=self._gateway_ready,
                gateway_disconnect_callback=self._gateway_ended,
            )
            with self._lock:
                if self._stopping:
                    return
                if not owned.authentication_live:
                    raise EmbeddedSshError(EmbeddedSshFailure.CONNECTION)
                self._connected = True
                size = self._size
                self._resize_pending = False
            terminal = await owned.open_terminal(*size)
            from termana.terminal.sftp import SftpRuntime

            runtime = SftpRuntime(owned, self._publish_sftp)
            proof = owned.take_proven_password()
            facts = owned.negotiated_facts
            with self._lock:
                if self._stopping:
                    return
                if not self._connected or not owned.authentication_live:
                    raise EmbeddedSshError(EmbeddedSshFailure.CONNECTION)
                self._proof = proof
                self._proof_deadline = min(
                    owned.password_proof_deadline,
                    time.monotonic() + EMBEDDED_GUI_PROOF_TIMEOUT_SECONDS,
                )
                self._ready = True
                self._negotiated_facts = facts
                self._sftp_runtime = runtime
            proof = None
            children = [
                asyncio.create_task(self._read_output(terminal)),
                asyncio.create_task(self._write_input(terminal)),
                asyncio.create_task(self._expire_proof()),
                asyncio.create_task(runtime.run()),
            ]
            # A writer failure must terminate the read, not linger indefinitely.
            done, _ = await asyncio.wait(children[:2], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            await terminal.wait_closed()
            with self._lock:
                self._exit_code = terminal.exit_status if terminal.exit_status is not None else 255
        except asyncio.CancelledError:
            pass
        except EmbeddedSshError as error:
            with self._lock:
                self._failure = error.failure
        except Exception:
            with self._lock:
                self._failure = EmbeddedSshFailure.CONNECTION
        finally:
            # A worker still releasing channels/threads is not a live save
            # owner. Revoke authorization before any awaited teardown.
            self._authentication_ended()
            for task in children:
                task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            try:
                if runtime is not None:
                    await runtime.close()
            finally:
                if owned is not None:
                    await owned.close()
            with self._lock:
                self._sftp_runtime = None

    def _authentication_ended(self) -> None:
        """Non-Qt worker callback: revoke authorization as soon as the client closes."""
        with self._lock:
            self._connected = self._ready = False
            self._negotiated_facts = None
            self._proof = None
            self._gateway_proof = None
            self._gateway_live = False
            self._request = None
            self._answer = _NO_ANSWER
            self._input.clear()
            self._input_bytes = 0

    def _gateway_ready(self, gateway):
        proof = gateway.take_proven_password()
        with self._lock:
            if self._stopping:
                return
            self._gateway_live = gateway.authentication_live
            self._gateway_proof = proof
            self._gateway_proof_deadline = gateway.password_proof_deadline

    def _gateway_ended(self):
        with self._lock:
            unexpected = self._gateway_live and not self._stopping
            self._gateway_live = False
            self._gateway_proof = None
            if unexpected:
                self._failure = EmbeddedSshFailure.CONNECTION
        if unexpected:
            self.close()

    async def _read_output(self, terminal) -> None:
        while data := await terminal.stdout.read(MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN):
            while True:
                self._output_space.clear()
                with self._lock:
                    if self._stopping:
                        return
                    if len(self._output) + len(data) <= MAX_EMBEDDED_PENDING_BYTES:
                        self._output.extend(data)
                        break
                await self._output_space.wait()

    async def _write_input(self, terminal) -> None:
        while True:
            self._input_event.clear()
            with self._lock:
                size = self._size if self._resize_pending else None
                self._resize_pending = False
                data = self._input.popleft() if self._input else None
            if size is not None:
                terminal.change_terminal_size(*size)
            if data is not None:
                terminal.stdin.write(data)
                await terminal.stdin.drain()
                with self._lock:
                    self._input_bytes = max(0, self._input_bytes - len(data))
                continue
            await self._input_event.wait()

    async def _expire_proof(self) -> None:
        with self._lock:
            remaining = self._proof_deadline - time.monotonic()
        await asyncio.sleep(max(0, remaining))
        with self._lock:
            self._proof = None

    @Slot()
    def _poll(self) -> None:
        with self._lock:
            progress = tuple(self._sftp_progress.values())
            results = tuple(self._sftp_results)
            self._sftp_progress.clear()
            self._sftp_results.clear()
            for result in results:
                self._sftp_pending.discard(result.request_id)
            request = None if self._stopping else self._request
            ready = self._ready and not self._stopping and not self._worker_done
            facts = self._negotiated_facts if ready and self._connected else None
            data = bytes(self._output[:MAX_OUTPUT_BYTES_PER_EVENT_LOOP_TURN])
            del self._output[: len(data)]
            if data:
                self._wake_locked(self._output_space)
            drained = not self._output
            done = self._worker_done
            if time.monotonic() >= self._proof_deadline:
                self._proof = None
            if time.monotonic() >= self._gateway_proof_deadline:
                self._gateway_proof = None
            gateway_ready = self._gateway_live and not self._stopping and not self._worker_done
            active = bool(self._sftp_pending or self._input_bytes or self._resize_pending)
        now = time.monotonic()
        if data or progress or results:
            self._active_until = now + EMBEDDED_ACTIVE_HOLD_SECONDS
        interval = (
            EMBEDDED_UI_POLL_MS
            if not ready or request is not None or active or now < self._active_until
            else EMBEDDED_IDLE_POLL_MS
        )
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        for event in progress:
            self.sftp_progress.emit(event)
        for event in results:
            self.sftp_result.emit(event)
        if gateway_ready and not self._gateway_published:
            self._gateway_published = True
            self.gateway_authenticated.emit()
        if self._published_request != request:
            previous, self._published_request = self._published_request, request
            if previous is not None:
                self.secret_request_finished.emit(previous)
            if request is not None:
                self.secret_requested.emit(request)
        if ready and not self._started_emitted:
            self._started_emitted = True
            self.started.emit()
            self.authenticated.emit()
        if facts != self._published_facts:
            self._published_facts = facts
            self.negotiation_changed.emit()
        if data:
            self.data_received.emit(data)
        if done and drained and (self._thread is None or not self._thread.is_alive()):
            self._timer.stop()
            if not self._completion_emitted:
                self._completion_emitted = True
                if self._failure is not None:
                    self.error_occurred.emit(_FAILURE_MESSAGES[self._failure])
                self.finished.emit(self._exit_code)


_FAILURE_MESSAGES = {
    EmbeddedSshFailure.CONFIGURATION: "Embedded SSH settings are unsupported. Select system OpenSSH or a supported direct profile.",
    EmbeddedSshFailure.LIBRARY_CONTRACT: "The embedded SSH library needs a compatibility review. Select system OpenSSH.",
    EmbeddedSshFailure.HOST_KEY: "SSH host-key review did not complete, or the trust file changed/could not be updated safely. Verify the fingerprint through a trusted channel and connect again. No credentials were sent to the rejected SSH endpoint; a previously verified gateway may already have authenticated.",
    EmbeddedSshFailure.AUTHENTICATION: "SSH authentication failed. Check the login and selected credentials.",
    EmbeddedSshFailure.IDENTITY: "The selected SSH key or agent could not be used. Check the key file, its permissions, passphrase and agent availability.",
    EmbeddedSshFailure.TIMEOUT: "SSH connection timed out.",
    EmbeddedSshFailure.CONNECTION: "The SSH connection ended unexpectedly.",
    EmbeddedSshFailure.TUNNEL: "A confirmed SSH tunnel could not be opened.",
    EmbeddedSshFailure.TERMINAL: "The server did not provide an interactive SSH terminal.",
}
