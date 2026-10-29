"""Native DWM caption colors, without replacing Windows window chrome."""

import ctypes
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from weakref import WeakSet, ref

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from termana.theme import ThemeColors

if TYPE_CHECKING:
    from termana.windows_client_caption import WindowsClientCaptionController

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
WINDOWS_11_BUILD = 22000
SPI_GETHIGHCONTRAST = 0x0042
HCF_HIGHCONTRASTON = 1
_RGB = re.compile(r"#[0-9a-fA-F]{6}\Z")


def colorref(color: str) -> int:
    """Convert reviewed RGB roles to Windows' 0x00bbggrr COLORREF."""
    if not isinstance(color, str) or _RGB.fullmatch(color) is None:
        raise ValueError("title bar color must be #rrggbb")
    red, green, blue = (int(color[index : index + 2], 16) for index in (1, 3, 5))
    return red | green << 8 | blue << 16


@dataclass(frozen=True, slots=True)
class CaptionColors:
    background: int
    foreground: int
    border: int
    dark: bool

    @classmethod
    def from_theme(cls, colors: ThemeColors) -> "CaptionColors":
        background = colorref(colors.terminal.background)
        components = [(background >> shift) & 255 for shift in (0, 8, 16)]
        return cls(
            background,
            colorref(colors.terminal.foreground),
            colorref(colors.border),
            sum(components) < 3 * 128,
        )


class CaptionApi(Protocol):
    build: int

    def high_contrast(self) -> bool | None:
        """Return None when accessibility policy cannot be queried safely."""

    def set_attribute(self, hwnd: int, attribute: int, value: int) -> bool:
        """Return native HRESULT success, not assumed visual acceptance."""

    def refresh_client_caption(self, hwnd: int) -> None:
        """Invalidate our client-painted caption without changing activation."""

    def reconcile_client_frame(self, hwnd: int) -> bool:
        """Apply the final frame flags without moving, activating or restacking."""

    def caption_point(self, hwnd: int, position: int) -> tuple[int, int, int] | None:
        """Return device-unit client x/y and resize border for our live HWND."""


@dataclass(frozen=True, slots=True)
class CaptionResult:
    dark_mode_accepted: bool
    exact_colors_accepted: bool
    system_colors: bool


def apply_caption(api: CaptionApi, hwnd: int, colors: CaptionColors) -> CaptionResult:
    """Use exact supported colors on Win11; never disable native decorations."""
    if type(hwnd) is not int or hwnd <= 0:
        raise ValueError("a live native window handle is required")
    system_colors = api.high_contrast() is not False
    dark_applied = api.set_attribute(
        hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, int(colors.dark and not system_colors)
    )
    exact_applied = False
    if api.build >= WINDOWS_11_BUILD:
        results = [
            api.set_attribute(hwnd, attribute, DWMWA_COLOR_DEFAULT if system_colors else value)
            for attribute, value in (
                (DWMWA_CAPTION_COLOR, colors.background),
                (DWMWA_TEXT_COLOR, colors.foreground),
                (DWMWA_BORDER_COLOR, colors.border),
            )
        ]
        exact_applied = all(results) and not system_colors
        if not all(results) and not system_colors:
            # Never leave one custom color paired with a rejected counterpart
            # (for example light caption + white text). Restore native defaults.
            for attribute in (DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR, DWMWA_BORDER_COLOR):
                api.set_attribute(hwnd, attribute, DWMWA_COLOR_DEFAULT)
            system_colors = True
    return CaptionResult(dark_applied, exact_applied, system_colors)


class _DwmCaptionApi:
    """Load system DLLs only on Windows; all native integer widths are explicit."""

    def __init__(self) -> None:
        self.build = sys.getwindowsversion().build
        self._dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._dwm.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._dwm.DwmSetWindowAttribute.restype = ctypes.c_int32
        self._user.SystemParametersInfoW.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._user.SystemParametersInfoW.restype = ctypes.c_int32
        self._user.InvalidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32]
        self._user.InvalidateRect.restype = ctypes.c_int32
        self._user.UpdateWindow.argtypes = [ctypes.c_void_p]
        self._user.UpdateWindow.restype = ctypes.c_int32
        self._user.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_uint32,
        ]
        self._user.SetWindowPos.restype = ctypes.c_int32
        # Get/SetWindowLongPtr are aliases of the 32-bit APIs on x86.
        long_suffix = "PtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "W"
        self._get_window_style = getattr(self._user, "GetWindowLong" + long_suffix)
        self._get_window_style.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        self._get_window_style.restype = ctypes.c_ssize_t
        self._set_window_style = getattr(self._user, "SetWindowLong" + long_suffix)
        self._set_window_style.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_ssize_t]
        self._set_window_style.restype = ctypes.c_ssize_t
        self._user.ScreenToClient.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._user.ScreenToClient.restype = ctypes.c_int32
        self._user.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        self._user.GetDpiForWindow.restype = ctypes.c_uint32
        self._user.GetSystemMetricsForDpi.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        self._user.GetSystemMetricsForDpi.restype = ctypes.c_int32

    def high_contrast(self) -> bool | None:
        class HighContrast(ctypes.Structure):
            _fields_ = [
                ("size", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("scheme", ctypes.c_void_p),
            ]

        state = HighContrast()
        state.size = ctypes.sizeof(state)
        if not self._user.SystemParametersInfoW(
            SPI_GETHIGHCONTRAST, state.size, ctypes.byref(state), 0
        ):
            return None
        return bool(state.flags & HCF_HIGHCONTRASTON)

    def set_attribute(self, hwnd: int, attribute: int, value: int) -> bool:
        data = ctypes.c_uint32(value)
        return (
            self._dwm.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)
            )
            >= 0
        )

    def refresh_client_caption(self, hwnd: int) -> None:
        # Qt's expanded-caption buttons refresh on WM_PAINT, not on a QWidget
        # palette update alone. This targets only our existing client HWND.
        if type(hwnd) is not int or hwnd <= 0:
            return  # InvalidateRect(NULL, ...) would invalidate unrelated windows.
        if self._user.InvalidateRect(hwnd, None, False):
            self._user.UpdateWindow(hwnd)

    def caption_point(self, hwnd: int, position: int) -> tuple[int, int, int] | None:
        if type(hwnd) is not int or hwnd <= 0:
            return None

        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]

        # GET_X/Y_LPARAM semantics preserve negative multi-monitor coordinates.
        point = Point(
            ctypes.c_int16(position & 0xFFFF).value, ctypes.c_int16((position >> 16) & 0xFFFF).value
        )
        dpi = self._user.GetDpiForWindow(hwnd)
        if not dpi or not self._user.ScreenToClient(hwnd, ctypes.byref(point)):
            return None
        border = (
            self._user.GetSystemMetricsForDpi(33, dpi)  # SM_CYSIZEFRAME
            + self._user.GetSystemMetricsForDpi(92, dpi)  # SM_CXPADDEDBORDER
        )
        return point.x, point.y, max(0, border)

    def reconcile_client_frame(self, hwnd: int) -> bool:
        if type(hwnd) is not int or hwnd <= 0:
            return False
        if self.build < WINDOWS_11_BUILD and self.high_contrast() is False:
            # Qt's public TitleHint stays off to avoid a duplicate client title.
            # Win10 DWM nevertheless needs native WS_CAPTION to darken the resize
            # border. Retain every other style bit and the native resize/buttons;
            # do not make the window frameless or recreate/activate its HWND.
            style = self._get_window_style(hwnd, -16)  # GWL_STYLE
            if not style:
                return False
            caption = 0x00C00000  # WS_CAPTION
            if style & caption != caption:
                if not self._set_window_style(hwnd, -16, style | caption):
                    return False
        # QWidget/QWindow can finish applying caption flags after native frame
        # calculation. Recalculate that cache once after Show; repaint alone
        # leaves a classic caption over the already-painted client band on Win10.
        flags = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
        # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED
        return bool(self._user.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags))


class WindowsTitlebarController(QObject):
    """Recolor existing top-level HWNDs; never create/activate a new window."""

    def __init__(
        self,
        application: QApplication,
        colors: ThemeColors,
        api: CaptionApi,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent if parent is not None else application)
        self._application = application
        self._colors = CaptionColors.from_theme(colors)
        self._api = api
        self._pending: WeakSet[QWidget] = WeakSet()
        application.installEventFilter(self)
        self.set_colors(colors)

    def set_colors(self, colors: ThemeColors) -> None:
        """Apply preview/commit/rollback alike, including already-open dialogs."""
        self._colors = CaptionColors.from_theme(colors)
        for widget in self._application.topLevelWidgets():
            self._schedule(widget)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QWidget) and event.type() in {
            QEvent.Type.Show,
            QEvent.Type.WinIdChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.ThemeChange,
            QEvent.Type.WindowActivate,
            QEvent.Type.WindowStateChange,
        }:
            self._schedule(watched)
        return False

    def _schedule(self, widget: QWidget) -> None:
        if not widget.isWindow() or widget in self._pending:
            return
        if widget.windowType() in (Qt.WindowType.Popup, Qt.WindowType.ToolTip):
            return
        self._pending.add(widget)
        reference = ref(widget)
        QTimer.singleShot(0, self, lambda: self._apply(reference))

    def _apply(self, reference) -> None:
        widget = reference()
        if widget is None or not isValid(widget):
            return
        self._pending.discard(widget)
        # effectiveWinId does not allocate a fresh HWND for a hidden/new widget.
        if not widget.isVisible() or widget.windowHandle() is None:
            return
        hwnd = int(widget.effectiveWinId())
        if hwnd:
            try:
                apply_caption(self._api, hwnd, self._colors)
            except OSError:
                # DWM unavailability must not prevent terminal use or destroy chrome.
                return


def create_titlebar_controller(
    application: QApplication, colors: ThemeColors, *, parent: QObject | None = None
) -> "WindowsTitlebarController | WindowsClientCaptionController | None":
    """No Windows APIs or global event filters are installed on other platforms."""
    if sys.platform != "win32" or not isinstance(application, QApplication):
        return None
    try:
        api = _DwmCaptionApi()
        if api.build < WINDOWS_11_BUILD and api.high_contrast() is False:
            from termana.windows_client_caption import (
                WindowsClientCaptionController,
                supports_client_caption,
            )

            if supports_client_caption():
                return WindowsClientCaptionController(application, colors, api, parent=parent)
        return WindowsTitlebarController(application, colors, api, parent=parent)
    except (OSError, AttributeError):
        return None
