"""Publish a complete edited copy through its exact live Windows workspace owner."""

import os
import stat
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from termana.credentials.windows_private_files import (
    _identity,
    _open,
    _private_descriptor,
    _rollback_created,
    _write_api,
)
from termana.credentials.windows_ssh_files import (
    WindowsSshFileError,
    windows_handle_security_stamp,
)
from termana.models.sftp import MAX_SFTP_EDIT_BYTES


@dataclass(frozen=True, slots=True, eq=False, init=False)
class WindowsEditDownloadTarget:
    """Opaque owner capability, never a pathname-based workspace adoption token."""

    _path: Path = field(repr=False)
    _directory_guard: Callable[[], AbstractContextManager[int]] = field(repr=False)

    def __init__(self, workspace, path: Path) -> None:
        from termana.terminal.windows_edit_workspace import WindowsEditWorkspace

        if type(workspace) is not WindowsEditWorkspace or path != Path(workspace.name) / "copy.txt":
            raise ValueError("An exact private edited-copy owner is required")
        object.__setattr__(self, "_path", path)
        # Retain the non-QObject owner. Its existing guard checks the original
        # handle/identity/security and refuses a closed or changed workspace.
        object.__setattr__(self, "_directory_guard", workspace.directory)

    def matches(self, path: Path) -> bool:
        """A capability cannot be retargeted to another request's local path."""
        return path == self._path

    def publish(self, data: bytes) -> None:
        """Create only copy.txt, without link/rename, replacement or prior truncation.

        The complete bounded payload is already in memory. No async operation
        occurs here, and the GUI cannot open the copy before worker success.
        A pre-editor crash may leave an unregistered private copy; recovery must
        preserve it rather than adopt or delete it by filename.
        """
        if os.name != "nt" or type(data) is not bytes or len(data) > MAX_SFTP_EDIT_BYTES:
            raise ValueError("A bounded private Windows edited copy is required")
        import msvcrt

        handle = descriptor = None
        try:
            with self._directory_guard() as parent, _private_descriptor() as security:
                # FILE_CREATE rejects every existing object. The original
                # DELETE/share3 parent pin stays open; no DELETE reopen is used.
                handle = _open(
                    "copy.txt", parent=parent, create=True, security=security, writable=True
                )
                identity = _identity(handle)
                stamp = windows_handle_security_stamp(handle, private=True)
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size:
                    raise WindowsSshFileError()
                remaining = memoryview(data)
                while remaining:
                    count = os.write(descriptor, remaining)
                    if not 0 < count <= len(remaining):
                        raise OSError("Private edited-copy write could not be confirmed")
                    remaining = remaining[count:]
                os.fsync(descriptor)
                after = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(after.st_mode)
                    or after.st_nlink != 1
                    or after.st_size != len(data)
                    or _identity(handle) != identity
                    or windows_handle_security_stamp(handle, private=True) != stamp
                ):
                    raise WindowsSshFileError()
            # Keep the created handle owned through the parent's post-check.
            # A later close error is uncertain completion, never permission to
            # retry closing or delete a pathname which might now be foreign.
        except BaseException:
            if handle is not None:
                _rollback_created(handle)
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            elif handle is not None:
                _write_api().CloseHandle(handle)
