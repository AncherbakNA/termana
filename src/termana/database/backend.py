"""One command at a time, with a Qt mailbox and an owned background worker."""

import socket
import sqlite3
import ssl
import time
from collections import deque
from threading import Event, Lock, Thread
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from termana.database.driver import DatabaseDriver
from termana.database.results import DatabaseComponentError, ResultLimitError, validate_query
from termana.models.connection import ConnectionProfile, ConnectionProtocol


def database_failure(error):
    cause = error
    for _ in range(5):
        if isinstance(cause, ssl.SSLCertVerificationError):
            return "The database certificate or server name could not be verified. No plaintext fallback was attempted."
        if isinstance(cause, ssl.SSLError):
            return "Database TLS negotiation failed. Check the server and trusted CA."
        cause = cause.__cause__ or cause.__context__
        if cause is None:
            break
    if isinstance(error, DatabaseComponentError):
        return "The installed database driver requires a compatibility review. Use the supported Termana environment or package."
    if isinstance(error, ImportError):
        return "A required database driver is unavailable. Install the supported Termana environment or package."
    if isinstance(error, ssl.SSLCertVerificationError):
        return "The database certificate or server name could not be verified. No plaintext fallback was attempted."
    if isinstance(error, ssl.SSLError):
        return "Database TLS negotiation failed. Check the server and trusted CA."
    if isinstance(error, ResultLimitError):
        return "The database reply exceeded the supported limit. Reconnect and use a smaller SELECT with LIMIT."
    if isinstance(error, TimeoutError):
        return "The database operation timed out. Check the server and transaction state before retrying."
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorname", "SQLITE_ERROR")
        if type(code) is str and code.startswith("SQLITE_") and code.isascii() and code.replace("_", "").isalnum():
            return "SQLite rejected the operation (" + code + "). Review the SQL, permissions and transaction."
    code = error.args[0] if error.args else None
    if type(code) is dict:
        code = code.get("C")
    if (type(code) is int and 0 < code < 65536) or (type(code) is str and len(code) == 5 and code.isascii() and code.isalnum()):
        return "Database operation failed (" + str(code) + "). Review the SQL, permissions and transaction."
    return "Database operation failed. Check the connection, TLS mode, sign-in, SQL and permissions."


def is_sql_rejection(error, engine):
    if engine == "sqlite":
        return isinstance(error, sqlite3.Error)
    code = error.args[0] if error.args else None
    if engine == "postgresql":
        code = code.get("C") if type(code) is dict else None
        return type(code) is str and len(code) == 5 and not code.startswith(("08", "57P0"))
    return type(code) is int and 1000 <= code < 2000


class DatabaseBackend(QObject):
    connected = Signal()
    finished = Signal()
    result = Signal(object, str, object)
    failed = Signal(object, str)

    def __init__(self, profile, username="", password="", parent=None):
        super().__init__(parent)
        if type(profile) is not ConnectionProfile or profile.protocol is not ConnectionProtocol.DATABASE:
            raise ValueError("Expected a validated database profile")
        if (type(username) is not str or len(username.encode("utf-8")) > 512
                or any(c in username for c in "\r\n\0")
                or type(password) is not str or len(password.encode("utf-8")) > 4096 or "\0" in password):
            raise ValueError("Unsupported database sign-in")
        self.profile = profile
        self._username, self._password = username, password
        self._lock = Lock()
        self._stop, self._wake = Event(), Event()
        self._events = deque()
        self._sockets = set()
        self._thread = None
        self._driver = None
        self._command = None
        self._ready = self._busy = self._done = self._finished = False
        self._operation_started = 0.0
        self._timed_out = False
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)

    @property
    def stopped(self):
        return self._stop.is_set()

    @property
    def is_running(self):
        return self._thread is not None and not self._finished

    @property
    def ready(self):
        with self._lock:
            return self._ready and not self._busy and not self.stopped and not self._done

    def check_stopped(self):
        if self.stopped:
            raise InterruptedError

    def track_socket(self, sock):
        with self._lock:
            self._sockets.add(sock)
        if self.stopped:
            self.close()

    def start(self):
        if self._thread is not None or self.stopped:
            return
        self._thread = Thread(target=self._run, name="Termana-database")
        self._operation_started = time.monotonic()
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None
            self._done = True
            self._events.append(("error", None, "The database worker could not start."))
        self._timer.start()

    def submit(self, operation, query=""):
        if operation not in ("query", "tables", "columns", "commit", "rollback"):
            return None
        if operation == "query":
            validate_query(query)
        with self._lock:
            if not self._ready or self._busy or self.stopped or self._done:
                return None
            request_id = uuid4()
            self._command = (request_id, operation, query)
            self._busy = True
            self._operation_started = time.monotonic()
            self._wake.set()
            return request_id

    def close(self):
        self._stop.set()
        self._wake.set()
        self._password = ""
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
        driver = self._driver
        connection = (driver.connection if driver is not None and driver.options.engine == "sqlite" else None)
        if connection is not None:
            try:
                connection.interrupt()
            except sqlite3.Error:
                pass
        if self._thread is None:
            self._done = True
            self._timer.start()

    def _publish(self, event):
        with self._lock:
            self._events.append(event)

    def _run(self):
        try:
            self.check_stopped()
            self._driver = driver = DatabaseDriver(self.profile, self)
            password, self._password = self._password, ""
            try:
                driver.connect(self._username, password)
            finally:
                password = ""
                self._username = ""
            self.check_stopped()
            with self._lock:
                self._ready = True
            self._publish(("connected", None, None))
            while not self.stopped:
                self._wake.wait(0.1)
                with self._lock:
                    command, self._command = self._command, None
                    self._wake.clear()
                if command is None:
                    continue
                request_id, operation, query = command
                try:
                    self.check_stopped()
                    if operation == "query":
                        value = driver.execute(query)
                    elif operation == "tables":
                        value = driver.tables()
                    elif operation == "columns":
                        value = driver.columns(*query[:2])
                    elif operation == "commit":
                        value = driver.commit()
                    else:
                        value = driver.rollback()
                    self._publish((operation, request_id, value))
                except Exception as error:
                    if not self.stopped:
                        self._publish(("error", request_id, database_failure(error)))
                    # Protocol/network/limit failures leave reply/transaction
                    # ownership uncertain. A SQL rejection can be rolled back.
                    if not is_sql_rejection(error, self.profile.database_options.engine):
                        self.close()
                finally:
                    query = ""
                    command = None
        except Exception as error:
            if not self.stopped:
                self._publish(("error", None, database_failure(error)))
        finally:
            self._password = self._username = ""
            try:
                if self._driver is not None:
                    self._driver.close()
            except Exception:
                pass
            with self._lock:
                self._ready = False
                self._done = True

    def _poll(self):
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
            if any(request_id is not None for _, request_id, _ in events):
                self._busy = False
            done, busy, ready = self._done, self._busy, self._ready
        for operation, request_id, value in events:
            if operation == "connected":
                if not self.stopped:
                    self.connected.emit()
            elif operation == "error":
                self.failed.emit(request_id, value)
            else:
                self.result.emit(request_id, operation, value)
        timeout = self.profile.database_options.query_timeout if ready else self.profile.database_options.timeout
        if not done and not self.stopped and (busy or not ready) and time.monotonic() - self._operation_started > timeout:
            self._timed_out = True
            self.close()
            self.failed.emit(None, "Database operation timed out; the connection was closed. Check the server transaction state before retrying.")
        if done and (self._thread is None or not self._thread.is_alive()) and not self._finished:
            self._finished = True
            self._timer.stop()
            self.finished.emit()
