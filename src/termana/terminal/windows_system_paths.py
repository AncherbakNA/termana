"""Trusted Windows directory discovery, independent of inherited environment."""

import ctypes
import re
import sys
from pathlib import PureWindowsPath
from typing import Final

MAX_WINDOWS_DIRECTORY_CHARACTERS: Final = 32768


def _system_directory_query():
    query = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW
    query.argtypes = [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint32]
    query.restype = ctypes.c_uint32
    return query


def native_windows_system_directory() -> PureWindowsPath | None:
    """Return the OS-owned system directory, or fail closed without a fallback.

    No environment, registry, shell, executable search or filesystem lookup is used.
    A foreign-platform test must explicitly inject directory discovery instead.
    """
    if sys.platform != "win32":
        return None
    try:
        buffer = ctypes.create_unicode_buffer(MAX_WINDOWS_DIRECTORY_CHARACTERS)
        length = _system_directory_query()(buffer, len(buffer))
        if type(length) is not int or not 0 < length < len(buffer):
            return None
        raw = buffer.value
        if len(raw.encode("utf-16-le")) // 2 != length or any(ord(c) < 32 for c in raw):
            return None
        selected = PureWindowsPath(raw)
        if (
            not selected.is_absolute()
            or re.fullmatch(r"[A-Za-z]:", selected.drive) is None
            or any(":" in part for part in selected.parts[1:])
        ):
            return None
        return selected
    except (AttributeError, OSError, TypeError, ValueError):
        return None
