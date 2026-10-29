"""GUI-thread mailbox for one explicitly requested System OpenSSH file owner."""

import asyncio
from collections import deque
from threading import Lock, Thread
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from termana.credentials.ssh_auth import SshSecretRequest
from termana.models.sftp import MAX_SFTP_PENDING, SftpProgress, SftpRequest
from termana.terminal.sftp import SftpRuntime
from termana.terminal.system_sftp_connection import SystemSftpConnection

_NO_RESPONSE = object()
SYSTEM_SFTP_FAILURE = (
    "The separate System SFTP connection ended or could not open. "
    "Verify this server's key in the SSH terminal first, then check the username, "
    "OpenSSH configuration and SFTP access. The terminal was not reconnected. "
    "File operations were not replayed; inspect any partial destination."
)


class SystemSftpSession(QObject):
    started = Signal()
    finished = Signal(int)
    sftp_result = Signal(object)
    sftp_progress = Signal(object)
    secret_requested = Signal(object)
    secret_request_finished = Signal(object)

    connection_note = "SFTP · Separate System OpenSSH connection for this tab"

    def __init__(self, profile, program, parent=None, *, known_hosts=None, system_config=None):
        super().__init__(parent)
        self.profile, self.program = profile, program
        self.known_hosts = known_hosts
        self.system_config = system_config
        self._lock = Lock()
        self._thread = None
        self._loop = self._task = self._runtime = self._response_event = None
        self._stopping = self._ready = self._done = False
        self._failure = False
        self._pending = set()
        self._results = deque()
        self._progress = {}
        self._request = self._published_request = None
        self._answer = _NO_RESPONSE
        self._started_emitted = self._finished_emitted = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)

    @property
    def sftp_available(self):
        with self._lock:
            return self._ready and not self._stopping and not self._done

    @property
    def failure_text(self):
        return SYSTEM_SFTP_FAILURE if self._failure else ""

    @property
    def is_finished(self):
        return self._finished_emitted

    def start(self):
        if self._thread is not None or self._stopping:
            return
        self._thread = Thread(target=self._worker, name="Termana-System-SFTP", daemon=False)
        try:
            self._thread.start()
        except RuntimeError:
            self._done = self._failure = True
        self._timer.start()

    def request_sftp(self, request):
        with self._lock:
            if (
                type(request) is not SftpRequest
                or not self._ready
                or self._stopping
                or self._done
                or self._runtime is None
                or request.request_id in self._pending
                or len(self._pending) >= MAX_SFTP_PENDING
            ):
                return False
            self._pending.add(request.request_id)
            try:
                self._loop.call_soon_threadsafe(self._runtime.enqueue, request)
            except RuntimeError:
                self._pending.discard(request.request_id)
                return False
        return True

    def cancel_sftp(self, request_id):
        with self._lock:
            if request_id not in self._pending or self._done or self._runtime is None:
                return False
            try:
                self._loop.call_soon_threadsafe(self._runtime.cancel, request_id)
                return True
            except RuntimeError:
                return False

    def submit_secret(self, request_id, answer):
        try:
            valid = (
                type(answer) is str
                and not any(c in answer for c in "\r\n\0")
                and len(answer.encode("utf-8")) <= 1023
            )
        except UnicodeError:
            valid = False
        with self._lock:
            if (
                not valid
                or self._stopping
                or self._done
                or self._request is None
                or self._request.request_id != request_id
                or self._answer is not _NO_RESPONSE
            ):
                return False
            self._answer = answer
            try:
                self._loop.call_soon_threadsafe(self._response_event.set)
                return True
            except RuntimeError:
                self._answer = _NO_RESPONSE
                return False

    def cancel_secret(self, request_id):
        with self._lock:
            current = self._request is not None and self._request.request_id == request_id
        if current:
            self.close()
        return current

    async def _response(self, prompt):
        endpoint = f"{self.profile.host}:{self.profile.port}"
        request = SshSecretRequest(
            uuid4(),
            endpoint,
            f"Separate System SFTP connection for {self.profile.username or 'configured user'}@{endpoint}.\n"
            f"Request from OpenSSH (server text, not instructions):\n{prompt}",
            self.profile.username,
            False,
            False,
        )
        with self._lock:
            if self._stopping:
                return None
            self._request = request
            self._answer = _NO_RESPONSE
        try:
            await self._response_event.wait()
            with self._lock:
                return None if self._stopping or self._answer is _NO_RESPONSE else self._answer
        finally:
            with self._lock:
                self._request = None
                self._answer = _NO_RESPONSE
            self._response_event.clear()

    def _publish(self, event):
        with self._lock:
            if event.request_id not in self._pending:
                return
            if isinstance(event, SftpProgress):
                self._progress[event.request_id] = event
            else:
                self._results.append(event)

    async def _run(self):
        connection = None
        children = []
        try:
            with self._lock:
                if self._stopping:
                    return
                self._loop = asyncio.get_running_loop()
                self._task = asyncio.current_task()
                self._response_event = asyncio.Event()
            connection = SystemSftpConnection(self.profile, self.program, self._response,
                                              known_hosts=self.known_hosts, system_config=self.system_config)
            await connection.connect()
            runtime = SftpRuntime(connection, self._publish)
            with self._lock:
                self._runtime = runtime
                self._ready = not self._stopping
            children = [
                asyncio.create_task(runtime.run()),
                asyncio.create_task(connection.process.wait()),
            ]
            await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            with self._lock:
                self._failure = not self._stopping
        except asyncio.CancelledError:
            pass
        except Exception:
            self._failure = True  # Never retain remote exceptions/argv/secret payloads.
        finally:
            with self._lock:
                self._ready = False
            cleanup = asyncio.create_task(self._retire(connection, children))
            # A close arriving after the transport has failed can cancel this
            # task while it is already in finally. Its private process/response
            # helper still must be reaped before the owning thread ends.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass
            await cleanup

    async def _retire(self, connection, children):
        try:
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            if connection:
                await connection.close()
        finally:
            with self._lock:
                self._ready = False
                self._request = None
                self._answer = _NO_RESPONSE

    def _worker(self):
        try:
            asyncio.run(self._run())
        except (Exception, asyncio.CancelledError):
            self._failure = True
        finally:
            with self._lock:
                self._done = True

    def close(self, *_ignored):
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._answer = _NO_RESPONSE
            if self._loop is not None and self._task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except RuntimeError:
                    pass

    def _poll(self):
        with self._lock:
            ready, done, request = self._ready, self._done, self._request
            progress, self._progress = self._progress, {}
            results, self._results = tuple(self._results), deque()
            for result in results:
                self._pending.discard(result.request_id)
        if self._published_request is not request:
            old, self._published_request = self._published_request, request
            if old is not None:
                self.secret_request_finished.emit(old)
            if request is not None and not self._stopping:
                self.secret_requested.emit(request)
        if ready and not self._started_emitted and not self._stopping:
            self._started_emitted = True
            self.started.emit()
        for event in progress.values():
            self.sftp_progress.emit(event)
        for result in results:
            self.sftp_result.emit(result)
        if (
            done
            and (self._thread is None or not self._thread.is_alive())
            and not self._finished_emitted
        ):
            self._finished_emitted = True
            self._timer.stop()
            self.finished.emit(1 if self._failure else 0)
