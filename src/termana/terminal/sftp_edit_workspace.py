"""Platform-protected edited-copy I/O; no truncation before identity/type checks."""

import hashlib
import logging
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from termana.models.sftp import MAX_SFTP_EDIT_BYTES
from termana.terminal.sftp_local_files import open_sftp_source

_LOG = logging.getLogger(__name__)


class PosixEditWorkspace:
    def __init__(self, store):
        from termana.terminal.private_recovery_files import PrivateDirectory

        self._store, self.recovery = store, None
        self.name = str(store.path / ("termana-edit-" + uuid4().hex))
        self._directory = PrivateDirectory(self.name, parent=store.directory, create=True)
        self.termana_directory_status = os.fstat(self._directory.handle)
        self.cleanup_failed = False

    @property
    def recovery_directory_handle(self):
        return self._directory.handle

    def checkpoint(self, data):
        return checkpoint_edit_copy(self, data)

    def preserve(self):
        if self.recovery is not None:
            self.recovery.close()
        if self._directory is not None:
            self._directory.close()
            self._directory = None
        self._store.close()

    def cleanup(self):
        if self._directory is None:
            return
        try:
            if self.recovery is None:
                if self._directory.names():
                    raise ValueError("Unknown private workspace entries")
            else:
                self.recovery.finish()
            self._directory.remove(self._store.directory)
        except Exception:
            self.cleanup_failed = True
            _LOG.warning("Private SFTP workspace cleanup could not be confirmed")
        finally:
            if self.recovery is not None:
                self.recovery.close()
            self._directory.close()
            self._directory = None
            self._store.close()

    def __del__(self):
        try:
            self.preserve()
        except Exception:
            pass


def create_edit_workspace():
    from termana.terminal.private_recovery import RecoveryStore

    store, workspace = None, None
    try:
        store = RecoveryStore()
        with store.allocation():
            if os.name == "nt":
                from termana.terminal.windows_edit_workspace import WindowsEditWorkspace

                workspace = WindowsEditWorkspace(parent=store.path)
                workspace._recovery_store = store
            elif os.name == "posix":
                workspace = PosixEditWorkspace(store)
            else:
                raise ValueError()
            workspace.recovery = store.create_editor(workspace)
        return workspace
    except Exception:
        if workspace is not None:
            workspace.cleanup()
        elif store is not None:
            store.close()
        raise ValueError("Private edit workspace could not be created safely") from None


def checkpoint_edit_copy(workspace, data):
    try:
        workspace.recovery.checkpoint(data)
        return True
    except Exception:
        _LOG.warning("Private editor recovery checkpoint could not be confirmed")
        return False


def _opened_copy(workspace, data):
    recovery = getattr(workspace, "recovery", None)
    if recovery is not None:
        recovery.register_copy()
        recovery.checkpoint(data)
    return data


@contextmanager
def private_edit_directory(workspace):
    descriptor = os.open(workspace.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.fstat(descriptor)
        expected = workspace.termana_directory_status
        if (
            (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
            or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            raise ValueError("Edited workspace changed")
        yield descriptor
    finally:
        os.close(descriptor)


def read_edit_copy(workspace, path, version):
    if os.name == "nt":
        from termana.credentials.windows_ssh_files import WindowsSshFileError

        try:
            return _opened_copy(workspace, workspace.read_copy(path, version))
        except WindowsSshFileError:
            raise ValueError("Edited copy could not be read safely") from None
    with private_edit_directory(workspace), open_sftp_source(path) as stream:
        data = stream.read(MAX_SFTP_EDIT_BYTES + 1)
    if len(data) != version.size or hashlib.sha256(data).digest() != version.digest:
        raise ValueError("Edited download changed before opening")
    return _opened_copy(workspace, data)


def write_edit_copy(workspace, path, data):
    if os.name == "nt":
        from termana.credentials.windows_ssh_files import WindowsSshFileError

        try:
            return workspace.write_copy(path, data)
        except WindowsSshFileError:
            raise ValueError("Edited copy could not be written safely") from None
    if (
        len(data) > MAX_SFTP_EDIT_BYTES
        or path.parent != Path(workspace.name)
        or path.name != "copy.txt"
    ):
        raise ValueError("Invalid edited workspace path")
    with private_edit_directory(workspace) as directory:
        descriptor = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o600
            ):
                raise ValueError("Edited copy must remain a private regular file")
            with os.fdopen(descriptor, "r+b", closefd=False) as stream:
                stream.write(data)
                stream.truncate()
        finally:
            os.close(descriptor)
