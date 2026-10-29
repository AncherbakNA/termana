"""Private Windows edited copies, pinned by an owned directory handle.

No recursive pathname cleanup, ACL repair, foreign-directory adoption or shell.
Unconfirmed cleanup preserves the private copy and emits only a fixed warning.
"""

import hashlib
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from termana.credentials.ssh_file_metadata import same_ssh_handle_status
from termana.credentials.windows_private_files import (
    _delete,
    _identity,
    _open,
    _private_descriptor,
    _rollback_created,
    _write_api,
    private_windows_directory,
)
from termana.credentials.windows_ssh_files import (
    WindowsSshFileError,
    windows_handle_security_stamp,
)
from termana.models.sftp import MAX_SFTP_EDIT_BYTES

_LOG = logging.getLogger(__name__)


class WindowsEditWorkspace:
    def __init__(self, *, parent=None):
        self._handle = None
        self.recovery = None
        self._recovery_store = None
        parent = Path(tempfile.gettempdir()) if parent is None else Path(parent)
        name = "termana-edit-" + uuid4().hex
        self.name = str(parent / name)
        self.cleanup_failed = False
        try:
            with private_windows_directory(parent) as handle, _private_descriptor() as security:
                self._handle = _open(
                    name,
                    parent=handle,
                    directory=True,
                    create=True,
                    security=security,
                    writable=True,
                    sharing=3,
                )
                self._identity = _identity(self._handle)
                self._security = windows_handle_security_stamp(self._handle, private=True)
        except BaseException:
            if self._handle is not None:
                try:
                    self.cleanup_failed = not _rollback_created(self._handle)
                finally:
                    _write_api().CloseHandle(self._handle)
                    self._handle = None
            raise

    @property
    def recovery_directory_handle(self):
        return self._handle

    def checkpoint(self, data):
        from termana.terminal.sftp_edit_workspace import checkpoint_edit_copy

        return checkpoint_edit_copy(self, data)

    def preserve(self):
        """Release ownership after an unreviewed teardown; never discard a draft."""
        if getattr(self, "recovery", None) is not None:
            self.recovery.close()
        if getattr(self, "_handle", None) is not None:
            _write_api().CloseHandle(self._handle)
            self._handle = None
        if getattr(self, "_recovery_store", None) is not None:
            self._recovery_store.close()

    @contextmanager
    def directory(self):
        if self._handle is None:
            raise ValueError("Edited workspace is closed")
        # The original share=3 handle still pins against rename/delete. This
        # metadata-only reopen must share its already-owned DELETE access.
        with private_windows_directory(Path(self.name), private=True, sharing=7) as reopened:
            if (
                _identity(reopened) != self._identity
                or windows_handle_security_stamp(self._handle, private=True) != self._security
            ):
                raise WindowsSshFileError()
            yield self._handle
            if (
                _identity(self._handle) != self._identity
                or windows_handle_security_stamp(self._handle, private=True) != self._security
            ):
                raise WindowsSshFileError()

    def _copy_name(self, path):
        if path != Path(self.name) / "copy.txt":
            raise ValueError("Invalid edited workspace path")
        return "copy.txt"

    @contextmanager
    def _copy(self, path, *, writable=False):
        import msvcrt

        with self.directory() as directory:
            handle = _open(self._copy_name(path), parent=directory, writable=writable)
            descriptor = None
            try:
                security = windows_handle_security_stamp(handle, private=True)
                flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY | os.O_NOINHERIT
                descriptor = msvcrt.open_osfhandle(handle, flags)
                with os.fdopen(descriptor, "r+b" if writable else "rb", closefd=False) as stream:
                    if os.fstat(stream.fileno()).st_nlink != 1:
                        raise ValueError("Edited copy must have exactly one link")
                    yield stream
                    if (
                        os.fstat(stream.fileno()).st_nlink != 1
                        or windows_handle_security_stamp(handle, private=True) != security
                    ):
                        raise WindowsSshFileError()
            finally:
                if descriptor is None:
                    _write_api().CloseHandle(handle)
                else:
                    os.close(descriptor)

    def read_copy(self, path, version):
        with self._copy(path) as stream:
            before = os.fstat(stream.fileno())
            data = stream.read(MAX_SFTP_EDIT_BYTES + 1)
            if (
                len(data) > MAX_SFTP_EDIT_BYTES
                or len(data) != version.size
                or hashlib.sha256(data).digest() != version.digest
                or not same_ssh_handle_status(before, os.fstat(stream.fileno()))
            ):
                raise ValueError("Edited download changed before opening")
            return data

    def write_copy(self, path, data):
        if type(data) is not bytes or len(data) > MAX_SFTP_EDIT_BYTES:
            raise ValueError("Invalid edited workspace content")
        with self._copy(path, writable=True) as stream:
            stream.write(data)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

    def cleanup(self):
        handle = getattr(self, "_handle", None)
        if handle is None:
            return
        try:
            if getattr(self, "recovery", None) is not None:
                # Journal refusal (including unknown .part files) preserves the
                # entire workspace. It never falls through to prefix cleanup.
                self.recovery.finish()
            with self.directory() as directory:
                names = [entry.name for entry in os.scandir(self.name)]
                if any(
                    name != "copy.txt"
                    and not (name.startswith(".termana-") and name.endswith(".part"))
                    for name in names
                ):
                    raise WindowsSshFileError()
                for name in names:
                    child = _open(name, parent=directory, writable=True)
                    try:
                        windows_handle_security_stamp(child, private=True)
                        _delete(child)
                    finally:
                        _write_api().CloseHandle(child)
            # Retain the namespace pin through deletion of the original object.
            _delete(handle)
        except (OSError, ValueError, WindowsSshFileError):
            self.cleanup_failed = True
            _LOG.warning("Private SFTP workspace cleanup could not be confirmed")
        finally:
            if getattr(self, "recovery", None) is not None:
                self.recovery.close()
            if getattr(self, "_recovery_store", None) is not None:
                self._recovery_store.close()
            if handle is not None:
                _write_api().CloseHandle(handle)
            self._handle = None

    def __del__(self):
        try:
            if getattr(self, "recovery", None) is not None:
                self.preserve()
            else:
                self.cleanup()
        except Exception:
            pass  # Interpreter teardown must not expose paths or exception data.
