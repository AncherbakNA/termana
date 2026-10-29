"""Win10 client-painted caption on Qt's system-managed window frame (Qt 6.9+).

Qt retains nonclient hit testing, the system menu and caption buttons. Only the
background and title are ours. A scoped hit test preserves native drag/Snap;
there are no frameless flags, synthetic input or manual drag loops.
"""

from dataclasses import dataclass
from weakref import WeakKeyDictionary, ref

from PySide6.QtCore import QEvent, QMargins, QObject, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QContextMenuEvent, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from termana.theme import ThemeColors
from termana.windows_caption_hit_test import CaptionHitTestFilter
from termana.windows_titlebar import CaptionApi, CaptionColors, WindowsTitlebarController

_MIN_CAPTION_HEIGHT = 30
_CLIENT_HINTS = getattr(Qt.WindowType, "ExpandedClientAreaHint", Qt.WindowType.Widget) | getattr(
    Qt.WindowType, "NoTitleBarBackgroundHint", Qt.WindowType.Widget
)
_WINDOW_TYPES = (Qt.WindowType.Window, Qt.WindowType.Dialog, Qt.WindowType.Tool)


def supports_client_caption() -> bool:
    """Never reinterpret the old, differently implemented Qt 6.8 flag alias."""
    from PySide6.QtCore import qVersion

    return tuple(int(part) for part in qVersion().split(".")[:2]) >= (6, 9)


class _CaptionBackground(QWidget):
    def __init__(self, window: QWidget, flags: Qt.WindowType) -> None:
        super().__init__(window)
        self._window = ref(window)
        self._flags = flags
        self._background = QColor()
        self._foreground = QColor()
        self.setObjectName("windowsClientCaption")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_colors(self, background: str, foreground: str) -> None:
        self._background, self._foreground = QColor(background), QColor(foreground)
        self.update()

    def _button_reserve(self) -> int:
        buttons = sum(
            bool(self._flags & flag)
            for flag in (
                Qt.WindowType.WindowMinimizeButtonHint,
                Qt.WindowType.WindowMaximizeButtonHint,
                Qt.WindowType.WindowCloseButtonHint,
            )
        )
        return round(self.height() * 1.5 * buttons) + 8

    def contains_title(self, position) -> bool:
        return (
            not self.isHidden()
            and 0 <= position.y() < self.height()
            and 0 <= position.x() < self.width() - self._button_reserve()
        )

    def paintEvent(self, event) -> None:
        window = self._window()
        if window is None or not isValid(window):
            return
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), self._background)
            # Match Qt's native caption hit targets; text must never cover them.
            right = self._button_reserve()
            left = 12
            icon = window.windowIcon()
            if not icon.isNull():
                icon.paint(painter, QRect(left, (self.height() - 16) // 2, 16, 16))
                left += 24
            title = window.windowTitle().replace("[*]", "*" if window.isWindowModified() else "")
            text_rect = self.rect().adjusted(left, 0, -right, 0)
            painter.setFont(self.font())
            painter.setPen(self._foreground)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                painter.fontMetrics().elidedText(
                    title, Qt.TextElideMode.ElideRight, max(0, text_rect.width())
                ),
            )
        finally:
            painter.end()


@dataclass
class _CaptionFrame:
    background: _CaptionBackground
    margins: QMargins
    reconciled_key: tuple[int, int] | None = None
    reconcile_pending: bool = False


class WindowsClientCaptionController(QObject):
    """Prepare hidden windows once, never recreate a visible HWND on theme changes."""

    def __init__(
        self,
        application: QApplication,
        colors: ThemeColors,
        api: CaptionApi,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent if parent is not None else application)
        self._application, self._api, self._colors = application, api, colors
        self._frames: WeakKeyDictionary[QWidget, _CaptionFrame] = WeakKeyDictionary()
        self._preparing = False
        self._native = WindowsTitlebarController(application, colors, api, parent=self)
        self.set_colors(colors)
        application.installEventFilter(self)
        reference = ref(self)

        def contains_caption(hwnd: int, position: int) -> bool:
            controller = reference()
            return (
                controller is not None
                and isValid(controller)
                and controller._contains_native_caption(hwnd, position)
            )

        self._hit_test = CaptionHitTestFilter(contains_caption)
        application.installNativeEventFilter(self._hit_test)
        hit_test = self._hit_test

        def remove_hit_test() -> None:
            # QApplication deletes its QObject children after its own C++
            # wrapper can already be invalid. Explicit controller deletion
            # must still detach the live native filter while the app exists.
            if isValid(application):
                application.removeNativeEventFilter(hit_test)

        self.destroyed.connect(remove_hit_test)

    def _contains_native_caption(self, hwnd: int, position: int) -> bool:
        for window, frame in list(self._frames.items()):
            if (
                isValid(window)
                and window.isVisible()
                and not window.isFullScreen()
                and window.windowHandle() is not None
                and int(window.effectiveWinId()) == hwnd
            ):
                try:
                    point = self._api.caption_point(hwnd, position)
                except OSError:
                    return False
                if point is None:
                    return False
                x, y, border = point
                if not window.isMaximized() and (y < border or x < border):
                    return False  # Keep top/left resize edges with Qt.
                scale = window.devicePixelRatioF()
                return frame.background.contains_title(QPointF(x / scale, y / scale))
        return False

    def set_colors(self, colors: ThemeColors) -> None:
        self._colors = colors
        # Qt paints the system buttons; make their glyphs follow our contrast.
        contrast = self._api.high_contrast() is not False
        scheme = (
            Qt.ColorScheme.Unknown
            if contrast
            else (
                Qt.ColorScheme.Dark
                if CaptionColors.from_theme(colors).dark
                else Qt.ColorScheme.Light
            )
        )
        self._application.styleHints().setColorScheme(scheme)
        self._native.set_colors(colors)
        self._refresh_backgrounds()
        for window in list(self._frames):
            if isValid(window) and window.isVisible() and window.windowHandle() is not None:
                try:
                    self._api.refresh_client_caption(int(window.effectiveWinId()))
                except OSError:
                    pass  # A transient paint failure must not destroy or activate a window.

    def _refresh_backgrounds(self) -> None:
        contrast = self._api.high_contrast() is not False
        # Qt's expanded-caption buttons use black glyphs in Unknown/HC mode.
        # Keep a 21:1 readable fallback rather than black glyphs on a black band.
        background, foreground = (
            ("#ffffff", "#000000")
            if contrast
            else (self._colors.terminal.background, self._colors.terminal.foreground)
        )
        for frame in list(self._frames.values()):
            if isValid(frame.background):
                frame.background.set_colors(background, foreground)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(watched, QWidget) or not watched.isWindow():
            return False
        if watched.windowType() not in _WINDOW_TYPES:
            return False
        kind = event.type()
        if kind == QEvent.Type.Polish and not self._preparing:
            self._prepare(watched)
        if watched in self._frames:
            if (
                kind == QEvent.Type.ContextMenu
                and isinstance(event, QContextMenuEvent)
                and event.reason() == QContextMenuEvent.Reason.Mouse
                and self._frames[watched].background.contains_title(event.pos())
            ):
                # Windows already owns this title-bar menu. Do not also open
                # QMainWindow's toolbar menu at the same screen position.
                return True
            if kind == QEvent.Type.MouseButtonDblClick and isinstance(event, QMouseEvent):
                frame = self._frames[watched]
                band = frame.background
                if (
                    event.button() == Qt.MouseButton.LeftButton
                    and event.modifiers() == Qt.KeyboardModifier.NoModifier
                    and band.contains_title(event.position())
                    and band._flags & Qt.WindowType.WindowMaximizeButtonHint
                ):
                    watched.showNormal() if watched.isMaximized() else watched.showMaximized()
                    return True
            if kind in (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.WindowStateChange):
                # QWidget adds WindowTitleHint back when system buttons are
                # requested. QWindow's public flag API lets Qt keep the buttons
                # without painting a second title above our plain-text title.
                handle = watched.windowHandle()
                if handle is not None:
                    flags = int(handle.flags())
                    hints = 0 if watched.isFullScreen() else int(_CLIENT_HINTS)
                    desired = (flags & ~int(_CLIENT_HINTS | Qt.WindowType.WindowTitleHint)) | hints
                    if flags != desired:
                        # Qt's expanded-caption child stays visible in fullscreen
                        # unless the client-area hint is temporarily removed.
                        handle.setFlags(Qt.WindowType(desired))
                        # Qt can reset native style bits when leaving fullscreen.
                        self._frames[watched].reconciled_key = None
                        self._schedule_frame_reconcile(watched)
                self._layout_caption(watched)
                if kind == QEvent.Type.Show:
                    self._schedule_frame_reconcile(watched)
            elif kind in (
                QEvent.Type.WindowTitleChange,
                QEvent.Type.WindowIconChange,
                QEvent.Type.ModifiedChange,
            ):
                self._frames[watched].background.update()
            elif kind in (QEvent.Type.ApplicationPaletteChange, QEvent.Type.ThemeChange):
                self._refresh_backgrounds()
        return False

    def _schedule_frame_reconcile(self, window: QWidget) -> None:
        frame = self._frames[window]
        if frame.reconcile_pending:
            return
        frame.reconcile_pending = True
        reference = ref(window)
        QTimer.singleShot(0, self, lambda: self._reconcile_frame(reference))

    def _reconcile_frame(self, reference) -> None:
        window = reference()
        if window is None or not isValid(window) or window not in self._frames:
            return
        frame = self._frames[window]
        try:
            handle = window.windowHandle()
            if not window.isVisible() or window.isFullScreen() or handle is None:
                return
            # QWidget may restore its TitleHint while completing the first Show.
            # Reconcile only after the final public QWindow flags are in place.
            flags = int(handle.flags())
            desired = flags & ~int(Qt.WindowType.WindowTitleHint)
            if flags != desired:
                handle.setFlags(Qt.WindowType(desired))
            hwnd = int(window.effectiveWinId())
            key = (hwnd, desired)
            if hwnd <= 0 or frame.reconciled_key == key:
                return
            if self._api.reconcile_client_frame(hwnd):
                frame.reconciled_key = key
        except OSError:
            pass  # Keep the native window usable; a later Show may retry.
        finally:
            frame.reconcile_pending = False

    def _prepare(self, window: QWidget) -> None:
        if window in self._frames or window.isVisible():
            return
        if window.windowFlags() & (Qt.WindowType.FramelessWindowHint | _CLIENT_HINTS):
            return
        if self._api.high_contrast() is not False:
            return  # Preserve native High Contrast for windows not yet prepared.
        self._preparing = True
        try:
            flags, margins = window.windowFlags(), window.contentsMargins()
            # Invert the integer mask, not a binding-specific enum complement:
            # Win32 bindings otherwise discarded high bits (Customize/Close).
            window.setWindowFlags(
                Qt.WindowType(
                    int(flags | _CLIENT_HINTS | Qt.WindowType.CustomizeWindowHint)
                    & ~int(Qt.WindowType.WindowTitleHint)
                )
            )
            band = _CaptionBackground(window, flags)
            self._frames[window] = _CaptionFrame(band, margins)
            self._refresh_backgrounds()
            self._layout_caption(window)
        finally:
            self._preparing = False

    def _layout_caption(self, window: QWidget) -> None:
        frame = self._frames[window]
        handle = window.windowHandle()
        safe_top = handle.safeAreaMargins().top() if handle is not None else 0
        height = 0 if window.isFullScreen() else max(_MIN_CAPTION_HEIGHT, safe_top)
        margins = frame.margins
        desired = QMargins(
            margins.left(), margins.top() + height, margins.right(), margins.bottom()
        )
        if window.contentsMargins() != desired:
            window.setContentsMargins(desired)
        frame.background.setGeometry(0, 0, window.width(), height)
        frame.background.setVisible(bool(height))
        frame.background.raise_()
