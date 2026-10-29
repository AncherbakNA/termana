"""Bounded Qt worker for operations whose failure details must stay private."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from uuid import UUID

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class BackgroundOperationSignals(QObject):
    """Publish only an opaque operation identity and a successful result."""

    succeeded = Signal(object, object)
    failed = Signal(object)


class BackgroundOperation(QRunnable):
    """Run one zero-argument callable without exposing failure details.

    The creating QObject should keep this runnable alive until either completion
    signal is handled. With Qt's default ``AutoConnection``, emitting from a
    thread-pool worker queues delivery to bound slots owned by the GUI thread.
    """

    def __init__(
        self,
        operation_id: UUID,
        operation: Callable[[], object],
    ) -> None:
        """Retain one validated opaque identity and one single-use callable."""
        super().__init__()
        if not isinstance(operation_id, UUID):
            raise TypeError("Background operation ID must be a UUID")
        if not callable(operation):
            raise TypeError("Background operation must be callable")
        self._operation_id = operation_id
        self._operation: Callable[[], object] | None = operation
        self._claim_lock = Lock()
        self.signals = BackgroundOperationSignals()

    @Slot()
    def run(self) -> None:
        """Execute at most once and reduce every ``Exception`` to a generic signal."""
        with self._claim_lock:
            operation = self._operation
            self._operation = None
        if operation is None:
            return

        try:
            result = operation()
        except Exception:
            # Leave the active exception context before invoking any direct slot.
            # Otherwise a same-thread receiver could recover private backend detail
            # through ``sys.exception()`` even though it is not a signal argument.
            pass
        else:
            self.signals.succeeded.emit(self._operation_id, result)
            return
        self.signals.failed.emit(self._operation_id)


class ConfidentialBackgroundOperationSignals(QObject):
    """Publish completion without putting a confidential value in Qt's event queue."""

    succeeded = Signal(object)
    failed = Signal(object)


class ConfidentialBackgroundOperation(QRunnable):
    """Run one secret-producing callable behind an opaque one-shot mailbox.

    A successful queued signal carries only the operation UUID.  The GUI-thread
    owner must validate that UUID and its own lifecycle context before calling
    :meth:`take_result`.  The stored value is released exactly once and is never
    included in a signal argument or exception message.
    """

    _MISSING = object()

    def __init__(
        self,
        operation_id: UUID,
        operation: Callable[[], object],
    ) -> None:
        """Retain one opaque identity and one single-use confidential callable."""
        super().__init__()
        if not isinstance(operation_id, UUID):
            raise TypeError("Background operation ID must be a UUID")
        if not callable(operation):
            raise TypeError("Background operation must be callable")
        self._operation_id = operation_id
        self._operation: Callable[[], object] | None = operation
        self._result: object = self._MISSING
        self._abandoned = False
        self._lock = Lock()
        self.signals = ConfidentialBackgroundOperationSignals()

    @Slot()
    def run(self) -> None:
        """Execute at most once and publish only an opaque completion identity."""
        with self._lock:
            operation = self._operation
            self._operation = None
        if operation is None:
            return

        try:
            result = operation()
        except Exception:
            pass
        else:
            with self._lock:
                if self._abandoned:
                    result = None
                    return
                self._result = result
            result = None
            self.signals.succeeded.emit(self._operation_id)
            return
        self.signals.failed.emit(self._operation_id)

    def take_result(self, operation_id: UUID) -> object:
        """Consume the confidential result once after validating its exact UUID."""
        if operation_id != self._operation_id:
            raise ValueError("Background operation ID does not match")
        with self._lock:
            result = self._result
            if result is self._MISSING:
                raise RuntimeError("Background operation result is unavailable")
            self._result = self._MISSING
        return result

    def discard_result(self, operation_id: UUID) -> bool:
        """Release a completed confidential result without exposing its value."""
        if operation_id != self._operation_id:
            return False
        with self._lock:
            if self._result is self._MISSING:
                return False
            self._result = self._MISSING
        return True

    def abandon(self, operation_id: UUID) -> bool:
        """Tombstone a stale task before or after its confidential callable returns.

        If the callable has not started, it is dropped.  If it is already running,
        :meth:`run` observes the tombstone before storing or signalling its result.
        A result already waiting in the mailbox is cleared atomically.
        """
        if operation_id != self._operation_id:
            return False
        with self._lock:
            if self._abandoned:
                return False
            self._abandoned = True
            self._operation = None
            self._result = self._MISSING
        return True
