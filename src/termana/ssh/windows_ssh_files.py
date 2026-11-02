"""Read-only Windows SSH file policy, applied to the exact opened file handle.

No shell, privilege elevation, ACL repair or dependency on localized group names.
Only ordinary allow/deny DACLs are understood; effective unfamiliar ACEs fail closed.
This is a conservative policy, not a general Windows effective-access evaluator.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Final

MAX_SECURITY_DESCRIPTOR_BYTES: Final = 65536
MAX_SECURITY_ACES: Final = 4096
_SYSTEM_SID: Final = b"\x01\x01\x00\x00\x00\x00\x00\x05" + struct.pack("<I", 18)
_ADMINISTRATORS_SID: Final = b"\x01\x02\x00\x00\x00\x00\x00\x05" + struct.pack("<II", 32, 544)
_OWNER_RIGHTS_SID: Final = b"\x01\x01\x00\x00\x00\x00\x00\x03" + struct.pack("<I", 4)
_FILE_GENERIC_READ: Final = 0x00120089
_FILE_SHARE_READ: Final = 1
_FILE_OPEN: Final = 1
_FILE_OPEN_REPARSE_POINT: Final = 0x00200000
_FILE_SYNCHRONOUS_IO_NONALERT: Final = 0x20
_FILE_NON_DIRECTORY_FILE: Final = 0x40
_OBJ_CASE_INSENSITIVE: Final = 0x40
_OBJ_DONT_REPARSE: Final = 0x1000
_DIRECTORY: Final = 0x10
_REPARSE_POINT: Final = 0x400
_DACL_PRESENT: Final = 0x0004
_SELF_RELATIVE: Final = 0x8000
_INHERIT_ONLY: Final = 0x08
_LOCAL_DRIVE_TYPES: Final = frozenset((2, 3, 5, 6))  # Removable, fixed, CD-ROM, RAM disk.
_DEVICE_BASENAME: Final = re.compile(
    r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|(?:COM|LPT)[1-9¹²³]", re.IGNORECASE
)
# Data access or permission/ownership mutation is unsafe for an untrusted SID.
# READ_CONTROL, READ_ATTRIBUTES and SYNCHRONIZE alone expose only metadata.
_PRIVATE_UNTRUSTED_SAFE: Final = 0x00120080
_TRUST_UNTRUSTED_SAFE: Final = 0xA01200A9


class WindowsSshFileError(RuntimeError):
    def __init__(self, *, native_status: int | None = None) -> None:
        super().__init__("The SSH file could not be read safely.")
        self.native_status = native_status


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_void_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", ctypes.c_uint32),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatus(ctypes.Union):
    _fields_ = [("Status", ctypes.c_int32), ("Pointer", ctypes.c_void_p)]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Result", _IoStatus), ("Information", ctypes.c_size_t)]


def _sid_at(data: bytes, offset: int, end: int) -> bytes:
    if offset < 0 or offset + 8 > end or data[offset] != 1 or data[offset + 1] > 15:
        raise WindowsSshFileError()
    length = 8 + 4 * data[offset + 1]
    if offset + length > end:
        raise WindowsSshFileError()
    return data[offset : offset + length]


def validate_windows_ssh_descriptor(data: bytes, current_sid: bytes, *, private: bool) -> None:
    """Bounded parser for OS-produced self-relative owner/DACL snapshots.

    Deny ACEs never erase a risky allow: complex effective-access policies remain
    unsupported, rather than accidentally accepting an exposed private key.
    """
    if type(data) is not bytes or not 20 <= len(data) <= MAX_SECURITY_DESCRIPTOR_BYTES:
        raise WindowsSshFileError()
    revision, _, control, owner, _, _, dacl = struct.unpack_from("<BBHIIII", data)
    if revision != 1 or control & (_SELF_RELATIVE | _DACL_PRESENT) != (
        _SELF_RELATIVE | _DACL_PRESENT
    ):
        raise WindowsSshFileError()
    if owner < 20 or dacl < 20 or dacl + 8 > len(data):
        raise WindowsSshFileError()  # Missing/null DACL is not private/trusted.
    trusted = {current_sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
    if _sid_at(data, owner, len(data)) not in trusted:
        raise WindowsSshFileError()
    # S-1-3-4 applies to the actual object owner. It is safe only AFTER the
    # owner itself passed, never an eligible owner or arbitrary trusted group.
    trusted.add(_OWNER_RIGHTS_SID)
    acl_revision, _, size, count, _ = struct.unpack_from("<BBHHH", data, dacl)
    if (
        acl_revision not in (2, 4)
        or size < 8
        or dacl + size > len(data)
        or count > MAX_SECURITY_ACES
    ):
        raise WindowsSshFileError()
    position, end = dacl + 8, dacl + size
    safe = _PRIVATE_UNTRUSTED_SAFE if private else _TRUST_UNTRUSTED_SAFE
    for _ in range(count):
        if position + 4 > end:
            raise WindowsSshFileError()
        kind, flags, length = struct.unpack_from("<BBH", data, position)
        if length < 4 or length % 4 or position + length > end:
            raise WindowsSshFileError()
        if not flags & _INHERIT_ONLY:
            if kind not in (0, 1) or length < 16:
                raise WindowsSshFileError()
            access = struct.unpack_from("<I", data, position + 4)[0]
            sid = _sid_at(data, position + 8, position + length)
            if kind == 0 and sid not in trusted and access & ~safe:
                raise WindowsSshFileError()
        position += length


@lru_cache(maxsize=1)
def _api():
    if os.name != "nt":
        raise WindowsSshFileError()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    native = ctypes.WinDLL("ntdll", use_last_error=True)
    handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    pointer = ctypes.POINTER(handle)
    api = SimpleNamespace()
    signatures = (
        (
            native,
            "NtCreateFile",
            ctypes.c_int32,
            [
                pointer,
                dword,
                ctypes.POINTER(_ObjectAttributes),
                ctypes.POINTER(_IoStatusBlock),
                handle,
                dword,
                dword,
                dword,
                dword,
                handle,
                dword,
            ],
        ),
        (
            kernel,
            "CreateFileW",
            handle,
            [ctypes.c_wchar_p, dword, dword, handle, dword, dword, handle],
        ),
        (kernel, "CloseHandle", boolean, [handle]),
        (kernel, "GetFileType", dword, [handle]),
        (kernel, "GetDriveTypeW", dword, [ctypes.c_wchar_p]),
        (kernel, "QueryDosDeviceW", dword, [ctypes.c_wchar_p, ctypes.c_wchar_p, dword]),
        (kernel, "GetFileInformationByHandleEx", boolean, [handle, ctypes.c_int, handle, dword]),
        (kernel, "GetCurrentProcess", handle, []),
        (kernel, "LocalFree", handle, [handle]),
        (security, "OpenProcessToken", boolean, [handle, dword, pointer]),
        (
            security,
            "GetTokenInformation",
            boolean,
            [handle, ctypes.c_int, handle, dword, ctypes.POINTER(dword)],
        ),
        (security, "IsValidSid", boolean, [handle]),
        (security, "GetLengthSid", dword, [handle]),
        (
            security,
            "GetSecurityInfo",
            dword,
            [handle, ctypes.c_int, dword, pointer, pointer, pointer, pointer, pointer],
        ),
        (security, "IsValidSecurityDescriptor", boolean, [handle]),
        (security, "GetSecurityDescriptorLength", dword, [handle]),
    )
    for library, name, result, arguments in signatures:
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
        setattr(api, name, function)
    return api


def _current_user_sid(api) -> bytes:
    token, size = ctypes.c_void_p(), ctypes.c_uint32()
    if not api.OpenProcessToken(api.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise WindowsSshFileError()
    try:
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not 12 <= size.value <= MAX_SECURITY_DESCRIPTOR_BYTES:
            raise WindowsSshFileError()
        buffer = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, buffer, size.value, ctypes.byref(size)):
            raise WindowsSshFileError()
        sid = ctypes.c_void_p.from_buffer(buffer).value
        if not sid or not api.IsValidSid(sid):
            raise WindowsSshFileError()
        length = api.GetLengthSid(sid)
        start = ctypes.addressof(buffer)
        if not 8 <= length <= 68 or not start <= sid <= start + len(buffer) - length:
            raise WindowsSshFileError()
        return ctypes.string_at(sid, length)
    finally:
        api.CloseHandle(token)


def validate_windows_ssh_file_path(path: Path) -> PureWindowsPath:
    """Reject unsupported names/drives BEFORE querying metadata or opening a leaf.

    A Path.lstat on a UNC input already consults its network endpoint. GetDriveTypeW
    checks the explicit drive root, not a selected file; mapped remote/unknown drives
    are unsupported as well. The opener separately denies reparses for the whole lookup.
    """
    selected = PureWindowsPath(path)
    if (
        not selected.is_absolute()
        or not re.fullmatch(r"[A-Za-z]:", selected.drive)
        or len(selected.parts) < 2
        or any(
            part == ".."
            or part.endswith((".", " "))
            or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
            or _DEVICE_BASENAME.fullmatch(part.split(".", 1)[0].rstrip(" "))
            for part in selected.parts[1:]
        )
    ):
        raise WindowsSshFileError()  # No devices, alternate streams or UNC endpoints.
    if _api().GetDriveTypeW(selected.anchor) not in _LOCAL_DRIVE_TYPES:
        raise WindowsSshFileError()
    return selected


def native_windows_ssh_file_name(path: Path) -> str:
    """Resolve only the drive's DOS-device link, never filesystem reparses.

    Windows10 rejects even \\??\\C: under OBJ_DONT_REPARSE. A bounded current
    QueryDosDevice mapping to a local disk volume avoids that object-manager
    link while retaining the same kernel no-reparse policy for every path part.
    SUBST, redirectors and unfamiliar device mappings remain unsupported.
    """
    selected = validate_windows_ssh_file_path(path)
    target = ctypes.create_unicode_buffer(4096)
    length = _api().QueryDosDeviceW(selected.drive, target, len(target))
    if not 1 < length <= len(target) or not re.fullmatch(
        r"\\Device\\HarddiskVolume[0-9]+", target.value, re.IGNORECASE
    ):
        raise WindowsSshFileError()
    return target.value + str(selected)[len(selected.drive) :]


def open_windows_ssh_file(path: Path) -> int:
    """Open an existing local disk leaf with kernel-enforced no-reparse lookup.

    Share only reads while this short-lived handle is owned: no data writer,
    rename/delete or replacement is allowed. No pathname metadata preflight or
    manual parent walk: OBJ_DONT_REPARSE rejects parents during this same open.
    """
    native_name = native_windows_ssh_file_name(path)
    api = _api()
    try:
        length = len(native_name.encode("utf-16-le"))
    except UnicodeError:
        raise WindowsSshFileError() from None
    if length > 65532:
        raise WindowsSshFileError()
    buffer = ctypes.create_unicode_buffer(native_name)
    name = _UnicodeString(length, length + 2, ctypes.cast(buffer, ctypes.c_void_p))
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        None,
        ctypes.pointer(name),
        _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
        None,
        None,
    )
    opened, io_status = ctypes.c_void_p(), _IoStatusBlock()
    status = api.NtCreateFile(
        ctypes.byref(opened),
        _FILE_GENERIC_READ,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        _FILE_SHARE_READ,
        _FILE_OPEN,
        _FILE_OPEN_REPARSE_POINT | _FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT,
        None,
        0,
    )
    handle = opened.value
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise WindowsSshFileError(native_status=status & 0xFFFFFFFF)
    transferred = False
    try:
        if status != 0:
            raise WindowsSshFileError(native_status=status & 0xFFFFFFFF)
        attributes = (ctypes.c_uint32 * 2)()
        if (
            api.GetFileType(handle) != 1  # FILE_TYPE_DISK
            or not api.GetFileInformationByHandleEx(
                handle, 9, attributes, ctypes.sizeof(attributes)
            )
            or attributes[0] & (_DIRECTORY | _REPARSE_POINT)
        ):
            raise WindowsSshFileError()
        import msvcrt

        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        transferred = True  # os.close now owns CloseHandle, exactly once.
        return descriptor
    finally:
        if not transferred:
            api.CloseHandle(handle)


def windows_ssh_security_stamp(descriptor: int, *, private: bool) -> bytes:
    """Validate and return owner/DACL snapshot of this descriptor, never a reopened path."""
    import msvcrt

    return windows_handle_security_stamp(msvcrt.get_osfhandle(descriptor), private=private)


def windows_handle_security_stamp(handle: int, *, private: bool) -> bytes:
    """Apply the same bounded ACL policy to an already opened file or directory."""
    api = _api()
    security = ctypes.c_void_p()
    result = api.GetSecurityInfo(
        handle,
        1,
        0x00000005,
        None,
        None,
        None,
        None,
        ctypes.byref(security),
    )
    try:
        if result or not security.value or not api.IsValidSecurityDescriptor(security):
            raise WindowsSshFileError()
        size = api.GetSecurityDescriptorLength(security)
        if not 20 <= size <= MAX_SECURITY_DESCRIPTOR_BYTES:
            raise WindowsSshFileError()
        data = ctypes.string_at(security, size)
        validate_windows_ssh_descriptor(data, _current_user_sid(api), private=private)
        return data
    finally:
        if security.value:
            api.LocalFree(security)
