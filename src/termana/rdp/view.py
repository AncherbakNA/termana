"""Local desktop scaling/scrolling, never a remote resize or reconnect."""

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import QScrollArea


class RdpDesktopView(QScrollArea):
    viewport_resized = Signal()
    def __init__(self, surface, parent=None):
        super().__init__(parent)
        self.surface = surface
        self.zoom = None
        self.setObjectName("rdpDesktopScroll")
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWidget(surface)
        self.viewport().installEventFilter(self)
        surface.frame_changed.connect(self._resize_surface)

    def set_zoom(self, percent=None):
        if percent not in (None, 50, 75, 100, 125, 150, 200) or type(percent) is bool:
            raise ValueError("Unsupported desktop zoom")
        self.surface.release_input()
        self.zoom = percent
        self._resize_surface()

    def _resize_surface(self):
        if self.zoom is None or self.surface.frame_size.isEmpty():
            size = self.viewport().size()
        else:
            frame = self.surface.frame_size
            size = QSize(round(frame.width() * self.zoom / 100),
                         round(frame.height() * self.zoom / 100))
        self.surface.resize(size)

    def eventFilter(self, watched, event):
        if watched is self.viewport() and event.type() == QEvent.Type.Resize:
            self._resize_surface()
            self.viewport_resized.emit()
        return super().eventFilter(watched, event)
