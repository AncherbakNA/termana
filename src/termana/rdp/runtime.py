"""Private per-attempt runtime. No credential persistence, ACL repair or scavenging."""
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4


class RdpRuntimeError(ValueError):
    def __init__(self):
        super().__init__("A private RDP runtime could not be confirmed safely")


class _WindowsRuntime:
    def __init__(self, root):
        from termana.credentials.windows_private_files import (
            _identity,
            _open,
            _private_descriptor,
            _write_api,
            create_windows_parent,
            private_windows_directory,
        )
        from termana.credentials.windows_ssh_files import windows_handle_security_stamp

        self._stack = ExitStack()
        self._root = self._child = None
        self._leaf = self._identity = None
        self.name = None
        try:
            # Only one missing application parent is allowed. The exact native
            # creation helper refuses EEXIST races and does not repair an ACL.
            try:
                parent = self._stack.enter_context(private_windows_directory(root.parent))
            except FileNotFoundError:
                create_windows_parent(root.parent/"not-created")
                parent = self._stack.enter_context(private_windows_directory(root.parent))
            with _private_descriptor() as security:
                try:
                    self._root = _open(root.name, parent=parent, directory=True, sharing=3)
                except FileNotFoundError:
                    created = _open(root.name, parent=parent, directory=True, create=True,
                                    security=security, sharing=3)
                    try:
                        windows_handle_security_stamp(created, private=True)
                        identity = _identity(created)
                    finally:
                        _write_api().CloseHandle(created)
                    self._root = _open(root.name, parent=parent, directory=True, sharing=3)
                    if _identity(self._root)!=identity:
                        raise RdpRuntimeError() from None
                windows_handle_security_stamp(self._root, private=True)
                leaf = "attempt-"+uuid4().hex
                created = _open(leaf, parent=self._root, directory=True, create=True,
                                security=security, sharing=3)
                try:
                    windows_handle_security_stamp(created, private=True)
                    identity = _identity(created)
                finally:
                    _write_api().CloseHandle(created)
                # A long-lived DELETE-access handle would prevent both the
                # native helper and a second attempt from opening this tree
                # without broadening FILE_SHARE_DELETE (see KB035). Keep a
                # read-only/no-delete-sharing lease instead, identity checked.
                self._child = _open(leaf, parent=self._root, directory=True, sharing=3)
                if _identity(self._child)!=identity:
                    raise RdpRuntimeError()
                self._leaf, self._identity = leaf, identity
            windows_handle_security_stamp(self._child, private=True)
            self.name = str(root/leaf)
        except BaseException:
            for handle in (self._child, self._root):
                if handle is not None:
                    _write_api().CloseHandle(handle)
            self._stack.close()
            raise RdpRuntimeError() from None

    def cleanup(self):
        from termana.credentials.windows_private_files import _delete, _identity, _open, _write_api
        from termana.credentials.windows_ssh_files import windows_handle_security_stamp

        child, self._child = self._child, None
        if child is None:
            return
        budget = [128]

        def empty(handle, path, depth):
            windows_handle_security_stamp(handle, private=True)
            if depth>4:
                raise RdpRuntimeError()
            with os.scandir(path) as entries:
                for entry in entries:
                    budget[0] -= 1
                    if budget[0]<0 or entry.name in (".", "..") or any(c in entry.name for c in "\\/:\0"):
                        raise RdpRuntimeError()
                    attributes = entry.stat(follow_symlinks=False).st_file_attributes
                    if attributes&0x400:  # Junctions/symlinks/mount reparse points.
                        raise RdpRuntimeError()
                    directory = bool(attributes&0x10)
                    opened = _open(entry.name, parent=handle, directory=directory,
                                   writable=True, sharing=3)
                    try:
                        windows_handle_security_stamp(opened, private=True)
                        if directory:
                            empty(opened, path/entry.name, depth+1)
                        _delete(opened)
                    finally:
                        _write_api().CloseHandle(opened)
        try:
            # Called only after the child process has finished. Release our
            # read lease, then reopen DELETE access relative to the still-pinned
            # private root. A replacement cannot be removed: identity must match.
            _write_api().CloseHandle(child)
            child = None
            child = _open(self._leaf, parent=self._root, directory=True, writable=True, sharing=3)
            if _identity(child)!=self._identity:
                raise RdpRuntimeError()
            empty(child, Path(self.name), 0)
            _delete(child)
        except Exception:
            raise RdpRuntimeError() from None
        finally:
            try:
                if child is not None:
                    _write_api().CloseHandle(child)
                if self._root is not None:
                    _write_api().CloseHandle(self._root)
                    self._root = None
                self._stack.close()
            except Exception:
                raise RdpRuntimeError() from None


def runtime_directory(root: Path):
    if not root.is_absolute() or root.is_symlink():
        raise RdpRuntimeError()
    if os.name=="nt":
        return _WindowsRuntime(root)
    if os.name!="posix":
        raise RdpRuntimeError()
    # Refuse redirection/unsafe parents BEFORE any mkdir. parents=True would
    # both traverse a link before refusal and create missing parents with the
    # default mode rather than our requested private mode.
    if any(path.is_symlink() for path in (root,*root.parents)):
        raise RdpRuntimeError()
    parent = root.parent
    if not parent.exists():
        grandparent = parent.parent
        if not grandparent.is_dir():
            raise RdpRuntimeError()
        info = grandparent.stat()
        if info.st_uid!=os.getuid() or info.st_mode&0o022:
            raise RdpRuntimeError()
        parent.mkdir(mode=0o700)  # Exactly one new parent, no EEXIST adoption/repair.
    if not parent.is_dir():
        raise RdpRuntimeError()
    info = parent.stat()
    if info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise RdpRuntimeError()
    root.mkdir(mode=0o700, exist_ok=True)
    for directory in (root,parent):
        if directory.is_symlink() or not directory.is_dir():
            raise RdpRuntimeError()
        info = directory.stat()
        if info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise RdpRuntimeError()
    return tempfile.TemporaryDirectory(prefix="attempt-", dir=root)
