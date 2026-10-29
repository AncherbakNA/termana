"""Single-instance ownership for mutable application configuration."""

from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QLockFile


class ApplicationLockFailure(StrEnum):
    """Stable reasons that the application lock could not be acquired."""

    ALREADY_RUNNING = "already_running"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"


class ApplicationLockError(RuntimeError):
    """Explain why Termana cannot safely open its mutable configuration."""

    def __init__(self, failure: ApplicationLockFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


def acquire_application_lock(lock_path: Path) -> QLockFile:
    """Acquire one zero-wait process lock and return its live owner object."""
    if not lock_path.is_absolute():
        raise ValueError("The application lock path must be absolute.")

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as error:
        raise ApplicationLockError(
            ApplicationLockFailure.PERMISSION_DENIED,
            "Termana cannot write to its configuration folder. Check the folder "
            "permissions, then try again.",
        ) from error
    except OSError as error:
        raise ApplicationLockError(
            ApplicationLockFailure.UNAVAILABLE,
            f"Termana cannot prepare its configuration folder: {error}",
        ) from error

    lock = QLockFile(str(lock_path))
    if lock.tryLock(0):
        return lock

    if lock.error() == QLockFile.LockError.PermissionError:
        failure = ApplicationLockFailure.PERMISSION_DENIED
        message = (
            "Termana cannot create its instance lock in the configuration folder. "
            "Check the folder permissions, then try again."
        )
    elif lock.error() == QLockFile.LockError.LockFailedError:
        failure = ApplicationLockFailure.ALREADY_RUNNING
        message = (
            "Termana is already running. Close the other Termana window before "
            "starting another instance."
        )
    else:
        failure = ApplicationLockFailure.UNAVAILABLE
        message = (
            "Termana could not secure its configuration files for this session. "
            "No window was opened to avoid overwriting connection data."
        )
    raise ApplicationLockError(failure, message)
