"""Initial dialog fitting in logical pixels, including native decorations."""

from PySide6.QtCore import QRect, QSize
from PySide6.QtWidgets import QWidget

from termana.windows_window_bounds import fit_expanded_window


def bounded_dialog_frame(
    frame: QRect, client_size: QSize, minimum: QSize, available: QRect
) -> QRect:
    """Shrink only when necessary; never override the dialog's hard minimum."""
    if not available.isValid():
        return QRect(frame)
    border_width = max(0, frame.width() - client_size.width())
    border_height = max(0, frame.height() - client_size.height())
    width = max(minimum.width() + border_width, min(frame.width(), available.width()))
    height = max(minimum.height() + border_height, min(frame.height(), available.height()))
    x = max(available.left(), min(frame.left(), available.right() - width + 1))
    y = max(available.top(), min(frame.top(), available.bottom() - height + 1))
    return QRect(x, y, width, height)


def fit_window_to_screen(dialog: QWidget) -> None:
    """Call after initial exposure, when Qt knows the native frame size.

    This is not a resize/theme event policy: later user sizing must be retained.
    Unusually small screens below the layout minimum cannot be made to fit here.
    """
    if not dialog.isVisible() or dialog.isMaximized() or dialog.isFullScreen():
        return
    screen = dialog.screen()
    if screen is None:
        return
    minimum = dialog.minimumSize().expandedTo(dialog.minimumSizeHint()).expandedTo(QSize(1, 1))
    if fit_expanded_window(dialog, minimum, bounded_dialog_frame):
        return
    frame = dialog.frameGeometry()
    client = dialog.size()
    target = bounded_dialog_frame(frame, client, minimum, screen.availableGeometry())
    if target == frame:
        return
    borders = QSize(
        max(0, frame.width() - client.width()), max(0, frame.height() - client.height())
    )
    target_client = target.size() - borders
    if target_client != client:
        dialog.resize(target_client)
    current_frame = dialog.frameGeometry()
    if target.topLeft() != current_frame.topLeft():
        # Public widget position and the actual expanded frame may have a
        # constant offset. Move by the required delta instead of losing it.
        dialog.move(dialog.pos() + target.topLeft() - current_frame.topLeft())


# Existing dialog callers keep the same API; main-window startup shares the policy.
fit_dialog_to_screen = fit_window_to_screen
