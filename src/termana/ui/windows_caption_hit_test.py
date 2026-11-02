"""Own-window Win32 caption hit testing; no synthetic input or drag loop."""

import ctypes
from collections.abc import Callable

from PySide6.QtCore import QAbstractNativeEventFilter

WM_NCHITTEST = 0x0084
HTCAPTION = 2


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt", _Point),
        ("lPrivate", ctypes.c_uint32),
    ]


class CaptionHitTestFilter(QAbstractNativeEventFilter):
    """Return a native title-bar result only for a positively identified band."""

    def __init__(self, contains_caption: Callable[[int, int], bool]) -> None:
        super().__init__()
        self._contains_caption = contains_caption

    def nativeEventFilter(self, event_type, message):
        if event_type != b"windows_generic_MSG" or message is None:
            return False, 0
        # Shiboken VoidPtr truthiness checks its optional buffer size, not the
        # address. Native messages can have a live address with no buffer size.
        pointer = int(message)
        if pointer <= 0:
            return False, 0
        native = _Message.from_address(pointer)
        if (
            native.message == WM_NCHITTEST
            and native.hwnd
            and self._contains_caption(int(native.hwnd), native.lParam)
        ):
            return True, HTCAPTION
        return False, 0
