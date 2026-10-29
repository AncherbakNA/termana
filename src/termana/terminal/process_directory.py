"""Read only a local child's cwd, without commands, prompt parsing or shell hooks."""

import ctypes
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

DIRECTORY_READERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="termana-cwd")


def process_directory(pid: int) -> str | None:
    """Best-effort metadata, never a reason to interrupt terminal input."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            # A fixed OS utility, off the UI thread and with a hard deadline.
            result = subprocess.run(
                ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn0"],
                capture_output=True,
                timeout=1,
                check=False,
            )
            for field in result.stdout.split(b"\0"):
                if field.startswith(b"n/"):
                    return os.fsdecode(field[1:])
        elif sys.platform.startswith("linux"):
            return os.readlink(f"/proc/{pid}/cwd")
        elif sys.platform == "win32":
            return _windows_directory(pid)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return None
    return None


def _windows_directory(pid: int) -> str | None:
    """Read the child's bounded CURDIR from the native or WOW64 PEB, read-only.

    Windows does not expose a public GetCurrentDirectory(other_process). These
    NT layouts are best effort: failed reads/unsupported architectures return no
    metadata, with no injection, privilege changes or writes to the process.
    """
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    nt.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_void_p,
    ]
    nt.NtQueryInformationProcess.restype = wintypes.LONG
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        return None
    try:
        width = ctypes.sizeof(ctypes.c_void_p)
        if width != 8:
            return None
        info = (ctypes.c_size_t * 6)()
        if nt.NtQueryInformationProcess(handle, 0, info, ctypes.sizeof(info), None) < 0:
            return None
        peb = info[1]
        wow = ctypes.c_size_t()
        if (
            nt.NtQueryInformationProcess(handle, 26, ctypes.byref(wow), width, None) >= 0
            and wow.value
        ):
            peb, width = wow.value, 4

        def read(address, size):
            if not address or size > 65536:
                raise ValueError("Invalid process metadata")
            buffer = ctypes.create_string_buffer(size)
            count = ctypes.c_size_t()
            if not kernel.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(count)):
                raise OSError("Process metadata unavailable")
            if count.value != size:
                raise OSError("Incomplete process metadata")
            return buffer.raw

        parameters = int.from_bytes(read(peb + (0x20 if width == 8 else 0x10), width), "little")
        descriptor = read(parameters + (0x38 if width == 8 else 0x24), 16 if width == 8 else 8)
        length = int.from_bytes(descriptor[:2], "little")
        maximum = int.from_bytes(descriptor[2:4], "little")
        address = int.from_bytes(descriptor[8:16] if width == 8 else descriptor[4:8], "little")
        if length == 0 or length % 2 or length > maximum or length > 32766:
            return None
        return read(address, length).decode("utf-16-le")
    finally:
        kernel.CloseHandle(handle)
