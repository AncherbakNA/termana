"""Qt mailbox for blocking library clients; workers never call Qt or native vaults.

The existing browser message names retain their Sftp prefix for compatibility.
These messages describe file operations, not a claim about the wire protocol.
"""

import ftplib
import ssl
from collections import deque
from queue import Empty, Queue
from threading import Event, Lock, Thread
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from termana.models.connection import ConnectionProfile
from termana.models.sftp import MAX_SFTP_PENDING, SftpProgress, SftpRequest, SftpResult
from termana.ssh.ssh_auth import SshSecretRequest


class JobCancelled(Exception):
    pass


class OverwriteRequired(Exception):
    pass


def failure_message(error, *, connecting=False):
    """Only fixed categories cross the GUI/log boundary, never server exception text."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return ("The server certificate could not be verified. Check the server name, certificate and trust configuration. No FTP password was sent."
                if connecting else "The file-data certificate could not be verified. Check the server certificate and destination state.")
    if isinstance(error, ssl.SSLError):
        return "TLS negotiation failed. Check the server's FTPS configuration; no plaintext fallback was attempted."
    if isinstance(error, TimeoutError):
        return "The file operation timed out. Check the connection and destination state before retrying."
    if isinstance(error, (PermissionError, FileNotFoundError)):
        return "The file or folder is unavailable or access was denied. Refresh and check permissions."
    if isinstance(error, ftplib.error_perm):
        code = str(error)[:3]
        if code in ("530", "532"):
            return "FTP sign-in was rejected. Check the username, password and server access policy."
        if code == "550":
            return "The remote file or folder is unavailable or access was denied."
    if isinstance(error, ImportError):
        return "A required file-transfer component is unavailable. Install a supported Termana package."
    return ("The file connection failed. Check the server, security mode and sign-in."
            if connecting else "File operation failed; check access, connection and destination state before retrying")


class FileWorker(QObject):
    started = Signal()
    finished = Signal(int)
    error_occurred = Signal(str)
    secret_requested = Signal(object)
    secret_request_finished = Signal(object)
    sftp_progress = Signal(object)
    sftp_result = Signal(object)

    def __init__(self, profile, username="", parent=None):
        super().__init__(parent)
        if type(profile) is not ConnectionProfile or profile.protocol is not self.protocol:
            raise ValueError("A file worker requires its exact validated protocol profile")
        self.profile = profile
        self.username = username
        self._lock = Lock()
        self._stop = Event()
        self._answer_ready = Event()
        self._answer = self._request = None
        self._queue = Queue(MAX_SFTP_PENDING)
        self._pending = set()
        self._cancelled = set()
        self._results = deque()
        self._progress = {}
        self._sockets = set()
        self._active = None
        self._thread = None
        self._ready = self._done = self._started_emitted = self._finished_emitted = False
        self._published_request = None
        self._failure = ""
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)

    @property
    def sftp_available(self):
        with self._lock:
            return self._ready and not self._stop.is_set() and not self._done

    @property
    def is_running(self):
        return self._thread is not None and not self._finished_emitted

    @property
    def current_secret_request(self):
        with self._lock:
            return self._request

    @property
    def file_protocol(self):
        return self.profile.protocol.upper()

    @property
    def has_pending_jobs(self):
        with self._lock:
            return bool(self._pending)

    def start(self):
        if self._thread is not None or self._stop.is_set():
            return
        self._thread = Thread(target=self._run, name=f"Termana-{self.profile.protocol.upper()}")
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None
            self._failure = "The file worker could not start. Close another connection and retry."
            self._done = True
        self._timer.start()

    def request_sftp(self, request):
        if type(request) is not SftpRequest:
            return False
        with self._lock:
            if (not self._ready or self._done or self._stop.is_set()
                    or request.request_id in self._pending or len(self._pending) >= MAX_SFTP_PENDING):
                return False
            self._pending.add(request.request_id)
            self._queue.put_nowait(request)
            return True

    def cancel_sftp(self, request_id):
        with self._lock:
            if request_id not in self._pending:
                return False
            self._cancelled.add(request_id)
            active = self._active == request_id
        if active:
            self._interrupt()
        return True

    def submit_secret(self, request_id, value):
        try:
            valid = (type(value) is str and len(value.encode("utf-8")) <= 4096
                     and not any(c in value for c in "\r\n\0"))
        except UnicodeError:
            valid = False
        if not valid:
            return False
        with self._lock:
            if (self._stop.is_set() or self._request is None
                    or self._request.request_id != request_id or self._answer_ready.is_set()):
                return False
            self._answer = value
            self._answer_ready.set()
            return True

    def _password(self):
        request = SshSecretRequest(
            uuid4(), f"{self.profile.host}:{self.profile.port}", "FTP password:",
            self.username, False,
        )
        with self._lock:
            if self._stop.is_set():
                raise JobCancelled
            self._request = request
            self._answer_ready.clear()
        try:
            if not self._answer_ready.wait(300) or self._stop.is_set():
                raise JobCancelled
            with self._lock:
                answer, self._answer = self._answer, None
            return answer
        finally:
            with self._lock:
                self._answer = self._request = None

    def _check_cancel(self, request):
        with self._lock:
            cancelled = request.request_id in self._cancelled
        if self._stop.is_set() or cancelled:
            raise JobCancelled

    def _track_socket(self, sock):
        if sock is not None:
            with self._lock:
                self._sockets.add(sock)
                cancelled = self._active in self._cancelled
            if self._stop.is_set() or cancelled:
                self._interrupt()

    def _interrupt(self):
        import socket

        with self._lock:
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        with self._lock:
            self._answer_ready.set()
        self._interrupt()
        if self._thread is None:
            self._done = True
            self._timer.start()

    def _publish(self, event):
        with self._lock:
            if isinstance(event, SftpProgress):
                self._progress[event.request_id] = event
            else:
                self._results.append(event)

    def _connect(self):
        raise NotImplementedError

    def _perform(self, request):
        raise NotImplementedError

    def _disconnect(self):
        self._interrupt()

    def _run(self):
        try:
            self._connect()
            with self._lock:
                self._ready = not self._stop.is_set()
            while not self._stop.is_set():
                try:
                    request = self._queue.get(timeout=0.1)
                except Empty:
                    continue
                with self._lock:
                    self._active = request.request_id
                try:
                    self._check_cancel(request)
                    result = self._perform(request)
                except JobCancelled:
                    result = SftpResult(request.request_id, "cancelled", "Cancelled; check destination state")
                except OverwriteRequired:
                    result = SftpResult(request.request_id, "overwrite-required", "Destination exists")
                except Exception as error:
                    with self._lock:
                        cancelled = request.request_id in self._cancelled
                    result = SftpResult(
                        request.request_id, "cancelled" if cancelled else "error",
                        "Cancelled; check destination state" if cancelled else
                        failure_message(error),
                    )
                self._publish(result)
                with self._lock:
                    self._active = None
                    self._sockets = {s for s in self._sockets if s.fileno() >= 0}
        except JobCancelled:
            pass
        except Exception as error:
            if not self._stop.is_set():
                self._failure = failure_message(error, connecting=True)
        finally:
            try:
                self._disconnect()
            except Exception:
                self._interrupt()
                if not self._stop.is_set():
                    self._failure = "File connection cleanup failed; check destination state before retrying."
            with self._lock:
                self._ready = False
                self._request = self._answer = None
                completed = {r.request_id for r in self._results}
                for request_id in self._pending - completed:
                    self._results.append(SftpResult(request_id, "cancelled", "File connection closed; check destination state"))
                self._done = True

    def _poll(self):
        with self._lock:
            progress, results = tuple(self._progress.values()), tuple(self._results)
            self._progress.clear()
            self._results.clear()
            for result in results:
                self._pending.discard(result.request_id)
                self._cancelled.discard(result.request_id)
            request = None if self._stop.is_set() else self._request
            ready = self._ready and not self._stop.is_set() and not self._done
            done = self._done
        for event in progress:
            self.sftp_progress.emit(event)
        for result in results:
            self.sftp_result.emit(result)
        if request != self._published_request:
            previous, self._published_request = self._published_request, request
            if previous is not None:
                self.secret_request_finished.emit(previous)
            if request is not None:
                self.secret_requested.emit(request)
        if ready and not self._started_emitted:
            self._started_emitted = True
            self.started.emit()
        if done and (self._thread is None or not self._thread.is_alive()):
            self._timer.stop()
            if not self._finished_emitted:
                self._finished_emitted = True
                if self._failure:
                    self.error_occurred.emit(self._failure)
                self.finished.emit(1 if self._failure else 0)
