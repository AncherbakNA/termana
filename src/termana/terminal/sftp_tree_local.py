"""Anchored local directory operations for recursive SFTP, without following links.

POSIX operations use directory descriptors. Windows pins every ancestor against
rename/delete and uses the existing kernel no-reparse source reader. This is not
an interprocess content snapshot or an atomic compare-and-swap of existing files.
"""

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from termana.credentials.ssh_file_metadata import same_ssh_handle_status, same_ssh_path_status
from termana.models.sftp_batch import local_download_name
from termana.terminal.sftp_local_files import SftpSourceChangedError

_WINDOWS_DIRECTORY_READ = 0x001200A0
_WINDOWS_SHARE_READ_WRITE = 3  # No FILE_SHARE_DELETE: pins against rename/deletion.
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_DIRECTORY_FLAGS = 0x02000000 | 0x00200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT


class TreeLocalChangedError(ValueError):
    pass


def is_link(status):
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status, "st_file_attributes", 0) & 0x400)


def same_directory(before, after):
    return (
        stat.S_ISDIR(after.st_mode)
        and not is_link(after)
        and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    )


@dataclass
class LocalDirectory:
    path: Path
    descriptor: int | None

    def status(self):
        return os.fstat(self.descriptor) if self.descriptor is not None else self.path.lstat()

    def lstat(self, name):
        local_download_name(name)
        try:
            return (
                os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
                if self.descriptor is not None
                else (self.path / name).lstat()
            )
        except FileNotFoundError:
            return None

    def entries(self):
        return os.scandir(self.descriptor if self.descriptor is not None else self.path)

    def _open(self, name, flags, mode=0o600):
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        if self.descriptor is not None:
            return os.open(name, flags, mode, dir_fd=self.descriptor)
        return os.open(self.path / name, flags, mode)

    def _unlink(self, name):
        if self.descriptor is not None:
            os.unlink(name, dir_fd=self.descriptor)
        else:
            (self.path / name).unlink()

    @contextmanager
    def source(self, name, expected):
        selected = self.lstat(name)
        if selected is None or is_link(selected) or not same_ssh_path_status(expected, selected):
            raise SftpSourceChangedError()
        if os.name == "nt":
            from termana.credentials.windows_ssh_files import open_windows_ssh_file

            descriptor = open_windows_ssh_file(self.path / name)
        else:
            descriptor = self._open(name, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not same_ssh_path_status(expected, before):
                raise SftpSourceChangedError()
            yield source
            current = self.lstat(name)
            if (
                current is None
                or not same_ssh_handle_status(before, os.fstat(source.fileno()))
                or not same_ssh_path_status(before, current)
            ):
                raise SftpSourceChangedError()

    @contextmanager
    def writer(self, name, expected):
        local_download_name(name)
        current = self.lstat(name)
        if (expected is None) != (current is None) or (
            expected is not None and not same_ssh_path_status(expected, current)
        ):
            raise TreeLocalChangedError()
        if current is not None and (is_link(current) or not stat.S_ISREG(current.st_mode)):
            raise TreeLocalChangedError()
        staged = ".termana-tree-" + uuid4().hex + ".part"
        descriptor = self._open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        identity = os.fstat(descriptor)
        stream = os.fdopen(descriptor, "wb")
        try:
            yield stream
            if expected is not None and os.name != "nt":
                os.fchmod(stream.fileno(), stat.S_IMODE(expected.st_mode) & 0o777)
            stream.flush()
            stream.close()
            current = self.lstat(name)
            staged_status = self.lstat(staged)
            if (
                staged_status is None
                or (identity.st_dev, identity.st_ino)
                != (staged_status.st_dev, staged_status.st_ino)
                or is_link(staged_status)
                or ((expected is None) != (current is None))
                or (expected is not None and not same_ssh_path_status(expected, current))
            ):
                raise TreeLocalChangedError()
            if self.descriptor is not None:
                if expected is None:
                    os.link(
                        staged,
                        name,
                        src_dir_fd=self.descriptor,
                        dst_dir_fd=self.descriptor,
                        follow_symlinks=False,
                    )
                else:
                    os.replace(staged, name, src_dir_fd=self.descriptor, dst_dir_fd=self.descriptor)
            elif expected is None:
                os.link(self.path / staged, self.path / name)
            else:
                os.replace(self.path / staged, self.path / name)
        finally:
            stream.close()
            current = self.lstat(staged)
            if current is not None:
                if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                    # A foreign replacement is not ours to delete.
                    raise TreeLocalChangedError()
                self._unlink(staged)


@contextmanager
def local_directory(path, *, creation_root=None, created=None):
    """Pin a complete directory walk; create only within the selected target root."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) > 256:
        raise TreeLocalChangedError()
    if os.name == "nt":
        with _windows_directory(path, creation_root=creation_root, created=created) as value:
            yield value
        return
    descriptors = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path.anchor, flags)
        descriptors.append(descriptor)
        prefix = Path(path.anchor)
        for component in path.parts[1:]:
            prefix /= component
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if creation_root is None or not prefix.is_relative_to(creation_root):
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                if created is not None:
                    created(prefix)
                child = os.open(component, flags, dir_fd=descriptor)
            descriptor = child
            descriptors.append(descriptor)
        yield LocalDirectory(path, descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _windows_directory(path, *, creation_root, created):
    import ctypes

    from termana.credentials.windows_private_files import _open
    from termana.credentials.windows_ssh_files import _api, native_windows_ssh_file_name

    api = _api()
    # Resolve only the local drive mapping, not a filesystem reparse point.
    sentinel = "__termana_unused_name__"
    native = native_windows_ssh_file_name(Path(path.anchor) / sentinel)[: -len(sentinel)]
    root = api.CreateFileW(
        "\\\\?\\GLOBALROOT" + native,
        _WINDOWS_DIRECTORY_READ,
        _WINDOWS_SHARE_READ_WRITE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_DIRECTORY_FLAGS,
        None,
    )
    handles = []
    try:
        if root in (None, ctypes.c_void_p(-1).value):
            raise TreeLocalChangedError()
        handles.append(root)
        info = (ctypes.c_uint32 * 2)()
        if (
            api.GetFileType(root) != 1
            or not api.GetFileInformationByHandleEx(root, 9, info, ctypes.sizeof(info))
            or not info[0] & 0x10
            or info[0] & 0x400
        ):
            raise TreeLocalChangedError()
        prefix = Path(path.anchor)
        parent = root
        for component in path.parts[1:]:
            local_download_name(component)
            prefix /= component
            try:
                child = _open(component, directory=True, parent=parent, sharing=3)
            except FileNotFoundError:
                if creation_root is None or not prefix.is_relative_to(creation_root):
                    raise
                child = _open(component, directory=True, parent=parent, create=True, sharing=3)
                if created is not None:
                    created(prefix)
            handles.append(child)
            parent = child
        yield LocalDirectory(path, None)
    finally:
        for handle in reversed(handles):
            api.CloseHandle(handle)
