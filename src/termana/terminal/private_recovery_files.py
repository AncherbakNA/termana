"""Small private, no-follow I/O used only by the recovery journal.

Windows mutations use exact native handles. POSIX mutations are relative to a
validated private owned directory. No recursive deletion or permission repair.
"""

import hashlib
import os
import stat
from pathlib import Path


class RecoveryError(ValueError):
    def __init__(self):
        super().__init__("Private recovery could not be confirmed safely")


def _name(name):
    if type(name) is not str or not name or name in (".", "..") or any(c in name for c in "\\/:\0"):
        raise RecoveryError()
    return name


class PrivateFile:
    def __init__(self, directory, name, *, create=False, lease=False):
        self.directory, self.name, self.fd = directory, _name(name), None
        self.handle = None
        try:
            if os.name == "nt":
                import msvcrt

                from termana.credentials.windows_private_files import _open, _private_descriptor

                with _private_descriptor() as security:
                    self.handle = _open(
                        name,
                        parent=directory.handle,
                        create=create,
                        security=security,
                        writable=True,
                        sharing=0 if lease else 1,
                    )
                self.fd = msvcrt.open_osfhandle(
                    self.handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT
                )
            elif os.name == "posix":
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                if create:
                    flags |= os.O_CREAT | os.O_EXCL
                self.fd = os.open(name, flags, 0o600, dir_fd=directory.handle)
            else:
                raise RecoveryError()
            self.validate()
            if lease and os.name == "posix":
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    def validate(self):
        status = os.fstat(self.fd)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise RecoveryError()
        if os.name == "nt":
            from termana.credentials.windows_ssh_files import windows_handle_security_stamp

            windows_handle_security_stamp(self.handle, private=True)
        elif status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) != 0o600:
            raise RecoveryError()

    @property
    def identity(self):
        if os.name == "nt":
            from termana.credentials.windows_private_files import _identity

            return _identity(self.handle).hex()
        status = os.fstat(self.fd)
        return f"{status.st_dev:x}:{status.st_ino:x}"

    def read(self, maximum):
        self.validate()
        before = os.fstat(self.fd)
        if before.st_size > maximum:
            raise RecoveryError()
        os.lseek(self.fd, 0, os.SEEK_SET)
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(self.fd, min(65536, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(self.fd)
        self.validate()
        if len(data) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RecoveryError()
        return bytes(data)

    def rewrite(self, data):
        self.validate()
        os.lseek(self.fd, 0, os.SEEK_SET)
        view = memoryview(data)
        while view:
            count = os.write(self.fd, view)
            if count <= 0:
                raise RecoveryError()
            view = view[count:]
        os.ftruncate(self.fd, len(data))
        os.fsync(self.fd)
        self.validate()

    def delete(self):
        self.validate()
        if os.name == "nt":
            from termana.credentials.windows_private_files import _delete

            _delete(self.handle)
        else:
            status = os.stat(self.name, dir_fd=self.directory.handle, follow_symlinks=False)
            if (status.st_dev, status.st_ino) != (
                os.fstat(self.fd).st_dev,
                os.fstat(self.fd).st_ino,
            ):
                raise RecoveryError()
            os.unlink(self.name, dir_fd=self.directory.handle)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = self.handle = None
        elif self.handle is not None:
            from termana.credentials.windows_private_files import _write_api

            _write_api().CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class PrivateDirectory:
    def __init__(self, path, *, parent=None, create=False, writable=False, borrowed_handle=None):
        self.path, self.handle = Path(path), None
        self.borrowed = borrowed_handle is not None
        if not self.path.is_absolute():
            raise RecoveryError()
        try:
            if self.borrowed:
                self.handle = borrowed_handle
            elif os.name == "nt":
                from termana.credentials.windows_private_files import _open, _private_descriptor

                with _private_descriptor() as security:
                    self.handle = _open(
                        self.path.name if parent else self.path,
                        parent=parent.handle if parent else None,
                        directory=True,
                        create=create,
                        writable=writable,
                        security=security,
                        sharing=3 if create or writable else 7,
                    )
            elif os.name == "posix":
                selected = self.path.name if parent else self.path
                if create:
                    os.mkdir(selected, 0o700, dir_fd=parent.handle if parent else None)
                self.handle = os.open(
                    selected,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent.handle if parent else None,
                )
            else:
                raise RecoveryError()
            self.validate()
        except BaseException:
            self.close()
            raise

    def validate(self):
        if os.name == "nt":
            from termana.credentials.windows_ssh_files import windows_handle_security_stamp

            windows_handle_security_stamp(self.handle, private=True)
        else:
            status = os.fstat(self.handle)
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_uid != os.getuid()
                or stat.S_IMODE(status.st_mode) != 0o700
            ):
                raise RecoveryError()

    @property
    def identity(self):
        if os.name == "nt":
            from termana.credentials.windows_private_files import _identity

            return _identity(self.handle).hex()
        status = os.fstat(self.handle)
        return f"{status.st_dev:x}:{status.st_ino:x}"

    def names(self, maximum=128):
        self.validate()
        names = []
        with os.scandir(self.path if os.name == "nt" else self.handle) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > maximum:
                    raise RecoveryError()
        return tuple(names)

    def sync(self):
        if os.name == "posix":
            os.fsync(self.handle)

    def remove(self, parent):
        self.validate()
        if self.names():
            raise RecoveryError()
        if os.name == "nt":
            from termana.credentials.windows_private_files import _delete

            _delete(self.handle)
        else:
            status = os.stat(self.path.name, dir_fd=parent.handle, follow_symlinks=False)
            if f"{status.st_dev:x}:{status.st_ino:x}" != self.identity:
                raise RecoveryError()
            os.rmdir(self.path.name, dir_fd=parent.handle)

    def close(self):
        if self.handle is not None:
            if self.borrowed:
                pass
            elif os.name == "nt":
                from termana.credentials.windows_private_files import _write_api

                _write_api().CloseHandle(self.handle)
            else:
                os.close(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def security_digest(handle):
    from termana.credentials.windows_ssh_files import windows_handle_security_stamp

    return hashlib.sha256(windows_handle_security_stamp(handle, private=True)).hexdigest()
