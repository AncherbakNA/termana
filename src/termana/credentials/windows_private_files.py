"""Local NTFS private creation and handle-relative commit; no ACL repair or shell.

The directory handle pins the destination, and OBJ_DONT_REPARSE applies to each
lookup. New objects receive a protected DACL at creation, not after writing.
Existing-target replacement is guarded but is not an interprocess compare/swap.
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from termana.credentials.embedded_ssh_trust import MAX_EMBEDDED_TRUST_FILE_BYTES
from termana.credentials.ssh_file_metadata import same_ssh_path_status
from termana.credentials.windows_ssh_files import (
    WindowsSshFileError,
    _api,
    _current_user_sid,
    _IoStatusBlock,
    _ObjectAttributes,
    _UnicodeString,
    native_windows_ssh_file_name,
    validate_windows_ssh_file_path,
    windows_handle_security_stamp,
)

_LOG = logging.getLogger(__name__)
MAX_PRIVATE_FILE_BYTES = 2 * 1024 * 1024
_READ = 0x00120089
_DIRECTORY_READ = 0x001200A0  # Metadata/TRAVERSE only: no data-sharing conflict on rename.
_WRITE_DELETE = 0x0013019F
_CREATE = 2
_OPEN = 1
_NOT_FOUND = frozenset((0xC0000034, 0xC000003A))


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    ]


@lru_cache(maxsize=1)
def _write_api():
    api = _api()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    native = ctypes.WinDLL("ntdll", use_last_error=True)
    api.NtSetInformationFile = native.NtSetInformationFile
    api.NtSetInformationFile.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    api.NtSetInformationFile.restype = ctypes.c_int32
    api.SetFileInformationByHandle = kernel.SetFileInformationByHandle
    api.SetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    api.SetFileInformationByHandle.restype = ctypes.c_int
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW = (
        security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    )
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    return api


@contextmanager
def _private_descriptor():
    api = _write_api()
    sid = _current_user_sid(api)
    user = (
        "S-1-"
        + str(int.from_bytes(sid[2:8], "big"))
        + "".join("-" + str(value) for value in struct.unpack_from("<" + "I" * sid[1], sid, 8))
    )
    text = f"O:{user}D:P(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    descriptor = ctypes.c_void_p()
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        text, 1, ctypes.byref(descriptor), None
    ):
        raise WindowsSshFileError()
    try:
        yield descriptor.value
    finally:
        api.LocalFree(descriptor)


def _open(
    name, *, directory=False, parent=None, create=False, security=None, writable=False, sharing=1
):
    """Open exactly one no-reparse object; only FILE_CREATE may create anything."""
    if parent is None:
        native_name = native_windows_ssh_file_name(Path(name))
    else:
        if not name or name in (".", "..") or any(c in name for c in "\\/:\0"):
            raise WindowsSshFileError()
        native_name = name
    try:
        length = len(native_name.encode("utf-16-le"))
    except UnicodeError:
        raise WindowsSshFileError() from None
    if length > 65532:
        raise WindowsSshFileError()
    api = _write_api()
    buffer = ctypes.create_unicode_buffer(native_name)
    name_value = _UnicodeString(length, length + 2, ctypes.cast(buffer, ctypes.c_void_p))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes), parent, ctypes.pointer(name_value), 0x1040, security, None
    )
    handle, io = ctypes.c_void_p(), _IoStatusBlock()
    options = 0x20 | (1 if directory else 0x00200040)
    access = (
        _DIRECTORY_READ | (0x10000 if writable else 0)
        if directory
        else (_WRITE_DELETE if writable else _READ)
    )
    if create:
        access |= 0x10000  # DELETE permits exact-handle rollback of this new object.
    status = api.NtCreateFile(
        ctypes.byref(handle),
        access,
        ctypes.byref(attributes),
        ctypes.byref(io),
        None,
        0,
        sharing,
        _CREATE if create else _OPEN,
        options,
        None,
        0,
    )
    if status != 0:
        if handle.value not in (None, ctypes.c_void_p(-1).value):
            api.CloseHandle(handle)
        if status & 0xFFFFFFFF in _NOT_FOUND:
            raise FileNotFoundError()
        raise WindowsSshFileError()
    try:
        if handle.value in (None, ctypes.c_void_p(-1).value):
            raise WindowsSshFileError()
        info = (ctypes.c_uint32 * 2)()
        if (
            api.GetFileType(handle) != 1
            or not api.GetFileInformationByHandleEx(handle, 9, info, ctypes.sizeof(info))
            or info[0] & 0x400
            or bool(info[0] & 0x10) != directory
        ):
            raise WindowsSshFileError()
        return handle.value
    except BaseException:
        if handle.value not in (None, ctypes.c_void_p(-1).value):
            try:
                if create:
                    _rollback_created(handle.value)
            finally:
                api.CloseHandle(handle)
        raise


def _identity(handle):
    data = ctypes.create_string_buffer(24)  # FILE_ID_INFO: volume serial + 128-bit ID.
    if not _write_api().GetFileInformationByHandleEx(handle, 18, data, len(data)):
        raise WindowsSshFileError()
    return data.raw


@contextmanager
def private_windows_directory(path, *, private=False, sharing=3):
    handle = _open(path, directory=True, sharing=sharing)
    api = _write_api()
    try:
        security = windows_handle_security_stamp(handle, private=private)
        identity = _identity(handle)
        yield handle
        if (
            _identity(handle) != identity
            or windows_handle_security_stamp(handle, private=private) != security
        ):
            raise WindowsSshFileError()
    finally:
        api.CloseHandle(handle)


def can_create_windows_parent(path):
    try:
        with private_windows_directory(path.parent):
            return False
    except FileNotFoundError:
        try:
            with private_windows_directory(path.parent.parent):
                return True
        except (OSError, WindowsSshFileError):
            return False
    except (OSError, WindowsSshFileError):
        return False


def windows_trust_save_supported(path, *, missing):
    try:
        with private_windows_directory(path.parent):
            return True
    except FileNotFoundError:
        return missing and can_create_windows_parent(path)
    except (OSError, WindowsSshFileError):
        return False


def create_windows_parent(path):
    """Create only one consent-selected missing parent; never adopt an EEXIST."""
    handle = None
    try:
        with (
            private_windows_directory(path.parent.parent) as grandparent,
            _private_descriptor() as sd,
        ):
            handle = _open(
                path.parent.name, directory=True, parent=grandparent, create=True, security=sd
            )
            windows_handle_security_stamp(handle, private=True)
    except BaseException:
        if handle is not None:
            _rollback_created(handle)
        raise
    finally:
        if handle is not None:
            _write_api().CloseHandle(handle)


def _verify_target(parent, name, expected_data, expected_status):
    try:
        handle = _open(name, parent=parent)
    except FileNotFoundError:
        if expected_status is not None:
            raise WindowsSshFileError() from None
        return
    import msvcrt

    descriptor = None
    try:
        if expected_status is None:
            raise WindowsSshFileError()
        stamp = windows_handle_security_stamp(handle, private=False)
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            before = os.fstat(stream.fileno())
            if before.st_nlink != 1 or not same_ssh_path_status(before, expected_status):
                raise WindowsSshFileError()
            if stream.read(MAX_PRIVATE_FILE_BYTES + 1) != expected_data:
                raise WindowsSshFileError()
            if not same_ssh_path_status(
                os.fstat(stream.fileno()), expected_status
            ) or stamp != windows_handle_security_stamp(handle, private=False):
                raise WindowsSshFileError()
    finally:
        if descriptor is None:
            _write_api().CloseHandle(handle)
        else:
            os.close(descriptor)


def _rename(handle, parent, name, *, replace):
    if not name or name in (".", "..") or any(c in name for c in "\\/:\0"):
        raise WindowsSshFileError()
    data = name.encode("utf-16-le")
    size = ctypes.sizeof(_RenameInfo) + len(data)
    buffer = ctypes.create_string_buffer(size)
    record = _RenameInfo.from_buffer(buffer)
    record.ReplaceIfExists = replace
    # This exact stage handle was created under the still-pinned parent.
    # A simple name with NULL RootDirectory renames within that same directory,
    # without IopOpenLinkOrRenameTarget reopening a DELETE-held parent on Win10.
    # Never substitute a full pathname or loosen the parent's sharing guard.
    record.RootDirectory = None
    record.FileNameLength = len(data)
    ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.FileName.offset, data, len(data))
    io = _IoStatusBlock()
    status = _write_api().NtSetInformationFile(handle, ctypes.byref(io), buffer, size, 10)
    if status != 0:
        error = WindowsSshFileError()
        error.native_status = status & 0xFFFFFFFF
        raise error


def _delete(handle):
    remove = ctypes.c_ubyte(1)
    if not _write_api().SetFileInformationByHandle(handle, 4, ctypes.byref(remove), 1):
        raise WindowsSshFileError()


def _rollback_created(handle):
    """Best-effort deletion of an exact, exclusively created object, never a path."""
    try:
        _delete(handle)
    except Exception:
        _LOG.warning("Private Windows file cleanup could not be confirmed")
        return False
    return True


def commit_windows_trust(
    path, expected_data, expected_status, data, *, create_parent=False, directory=None
):
    """Private stage, snapshot refusal, handle-relative no-clobber/create or replace.

    A process crash may leave a private stage; no untrusted scavenging is attempted.
    Replace of an existing target is not an interprocess compare-and-swap.
    """
    if type(data) is not bytes or len(data) > MAX_EMBEDDED_TRUST_FILE_BYTES:
        raise WindowsSshFileError()
    validate_windows_ssh_file_path(path)
    if create_parent and directory is not None:
        raise WindowsSshFileError()
    if create_parent:
        create_windows_parent(path)
    _commit_windows_private(
        path, expected_data, expected_status, data, ".termana-host-key-", directory=directory
    )


def write_windows_private_export(path: Path, data: bytes) -> None:
    """Write a new private local export, <=2 MiB; never replace or create a parent."""
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or type(data) is not bytes
        or len(data) > MAX_PRIVATE_FILE_BYTES
    ):
        raise WindowsSshFileError()
    validate_windows_ssh_file_path(path)
    _commit_windows_private(path, b"", None, data, ".termana-export-")


@contextmanager
def _publication_parent(path, directory=None):
    """Reuse a live private parent without loosening its no-delete sharing guard."""
    if directory is None:
        with private_windows_directory(path.parent) as parent:
            yield parent
        return
    from termana.terminal.private_recovery_files import PrivateDirectory

    if type(directory) is not PrivateDirectory or directory.path != path.parent:
        raise WindowsSshFileError()
    directory.validate()
    handle, identity = directory.handle, directory.identity
    security = windows_handle_security_stamp(handle, private=True)
    yield handle
    directory.validate()
    if (
        directory.handle != handle
        or directory.identity != identity
        or windows_handle_security_stamp(handle, private=True) != security
    ):
        raise WindowsSshFileError()


def _commit_windows_private(
    path, expected_data, expected_status, data, stage_prefix, *, directory=None
):
    """Shared private-at-create stage and guarded handle-relative publication."""
    import msvcrt

    from termana.terminal.private_recovery import finish_stage, register_windows_stage

    with _publication_parent(path, directory) as parent, _private_descriptor() as sd:
        _verify_target(parent, path.name, expected_data, expected_status)
        stage_name = stage_prefix + uuid4().hex
        handle = _open(
            stage_name,
            parent=parent,
            create=True,
            security=sd,
            writable=True,
        )
        published, descriptor, receipt = False, None, None
        try:
            stamp = windows_handle_security_stamp(handle, private=True)
            try:
                # Persist exact parent/file identity and lease BEFORE payload.
                # Registration failure aborts; no unregistered data is written.
                receipt = register_windows_stage(
                    path.parent / stage_name,
                    parent,
                    handle,
                    publication_target=path if stage_prefix == ".termana-export-" else None,
                )
            except Exception:
                raise WindowsSshFileError() from None
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
            with os.fdopen(descriptor, "w+b", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                if stamp != windows_handle_security_stamp(handle, private=True):
                    raise WindowsSshFileError()
                _verify_target(parent, path.name, expected_data, expected_status)
                _rename(handle, parent, path.name, replace=expected_status is not None)
                published = True
        finally:
            try:
                removed = True
                if not published:
                    removed = _rollback_created(handle)
                if published or removed:
                    finish_stage(receipt)
                elif receipt is not None:
                    receipt.close()  # Preserve a registered exact rollback failure.
                    receipt.store.close()
            finally:
                if descriptor is None:
                    _write_api().CloseHandle(handle)
                else:
                    os.close(descriptor)
