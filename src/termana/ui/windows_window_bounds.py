"""Initial fitting of our expanded HWND without Qt's transient frame cache."""

import ctypes
import math
import sys
from functools import lru_cache

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QApplication, QWidget

MONITOR_DEFAULTTONEAREST = 2
SWP_NOZORDER_NOACTIVATE = 0x0004 | 0x0010


class _Rect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("left", "top", "right", "bottom")]

    def as_qrect(self):
        return QRect(self.left, self.top, self.right - self.left, self.bottom - self.top)


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_uint32),
    ]


class _WindowBoundsApi:
    def __init__(self, user32):
        self._user = user32
        for name in ("GetWindowRect", "GetClientRect"):
            method = getattr(user32, name)
            method.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Rect)]
            method.restype = ctypes.c_int32
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
        user32.GetMonitorInfoW.restype = ctypes.c_int32
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_uint32,
        ]
        user32.SetWindowPos.restype = ctypes.c_int32

    def fit(self, hwnd: int, minimum: QSize, bound_frame) -> bool:
        """Read and set only this HWND, in native pixels; never activate/reorder."""
        if type(hwnd) is not int or not 0 < hwnd < ctypes.c_void_p(-1).value:
            return False
        frame, client = _Rect(), _Rect()
        monitor = _MonitorInfo()
        monitor.cbSize = ctypes.sizeof(_MonitorInfo)
        if not self._user.GetWindowRect(hwnd, ctypes.byref(frame)):
            return False
        if not self._user.GetClientRect(hwnd, ctypes.byref(client)):
            return False
        display = self._user.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not display or not self._user.GetMonitorInfoW(display, ctypes.byref(monitor)):
            return False
        outer, inner, work = frame.as_qrect(), client.as_qrect(), monitor.rcWork.as_qrect()
        if not all(rect.isValid() for rect in (outer, inner, work)):
            return False
        target = bound_frame(outer, inner.size(), minimum, work)
        if target == outer:
            return True
        return bool(
            self._user.SetWindowPos(
                hwnd,
                None,
                target.x(),
                target.y(),
                target.width(),
                target.height(),
                SWP_NOZORDER_NOACTIVATE,
            )
        )


@lru_cache(maxsize=1)
def _window_bounds_api() -> _WindowBoundsApi | None:
    try:
        return _WindowBoundsApi(ctypes.WinDLL("user32"))
    except (AttributeError, OSError):
        return None


def fit_expanded_window(window: QWidget, minimum: QSize, bound_frame) -> bool:
    """Use OS frame/client/work-area coordinates only for Qt's expanded frame."""
    if sys.platform != "win32" or QApplication.platformName() != "windows":
        return False
    handle = window.windowHandle()
    hint = getattr(Qt.WindowType, "ExpandedClientAreaHint", Qt.WindowType.Widget)
    if handle is None or not hint or not handle.flags() & hint:
        return False
    ratio = window.devicePixelRatioF()
    if not math.isfinite(ratio) or ratio <= 0:
        return False
    api = _window_bounds_api()
    if api is None:
        return False
    pixels = QSize(math.ceil(minimum.width() * ratio), math.ceil(minimum.height() * ratio))
    try:
        return api.fit(int(window.effectiveWinId()), pixels, bound_frame)
    except (OSError, ValueError):
        return False
