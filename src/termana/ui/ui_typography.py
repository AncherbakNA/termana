"""Accessible typography helpers derived from the user's application font."""

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget

_FONT_SCALE_PROPERTY = "termanaFontScale"
_FONT_WEIGHT_PROPERTY = "termanaFontWeight"
_FONT_MONOSPACE_PROPERTY = "termanaFontMonospace"
_SCALER_PROPERTY = "termanaSemanticFontScaler"


def _scaled_application_font(
    widget: QWidget,
    factor: float,
    weight: QFont.Weight | None,
    *,
    monospace: bool = False,
) -> QFont:
    application = QApplication.instance()
    base_font = application.font() if isinstance(application, QApplication) else widget.font()
    font = QFont(base_font)
    point_size = font.pointSizeF()
    if point_size > 0:
        font.setPointSizeF(point_size * factor)
    else:
        pixel_size = font.pixelSize()
        if pixel_size > 0:
            font.setPixelSize(max(1, round(pixel_size * factor)))
    if weight is not None:
        font.setWeight(weight)
    if monospace:
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setFamilies(fixed.families())
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        font.setKerning(False)
    return font


class _SemanticFontScaler(QObject):
    """Refresh semantic labels after a live OS/application font change."""

    def __init__(self, application: QApplication) -> None:
        super().__init__(application)
        self._application = application
        self._refresh_pending = False
        application.fontChanged.connect(self._schedule_refresh)

    @Slot(QFont)
    def _schedule_refresh(self, _font: QFont) -> None:
        """Coalesce native font notifications without observing every Qt event."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(0, self._refresh_widgets)

    def _refresh_widgets(self) -> None:
        self._refresh_pending = False
        for widget in self._application.allWidgets():
            factor = widget.property(_FONT_SCALE_PROPERTY)
            if not isinstance(factor, float):
                continue
            weight_value = widget.property(_FONT_WEIGHT_PROPERTY)
            weight = QFont.Weight(weight_value) if isinstance(weight_value, int) else None
            widget.setFont(
                _scaled_application_font(
                    widget,
                    factor,
                    weight,
                    monospace=widget.property(_FONT_MONOSPACE_PROPERTY) is True,
                )
            )


def _ensure_semantic_font_scaler(application: QApplication) -> None:
    if application.property(_SCALER_PROPERTY) is not None:
        return
    scaler = _SemanticFontScaler(application)
    application.setProperty(_SCALER_PROPERTY, scaler)


def scale_widget_font(
    widget: QWidget,
    factor: float,
    *,
    weight: QFont.Weight | None = None,
    monospace: bool = False,
) -> None:
    """Scale live app typography, optionally choosing the system fixed-width family."""
    if factor <= 0:
        raise ValueError("Font scale factor must be positive")
    application = QApplication.instance()
    if isinstance(application, QApplication):
        _ensure_semantic_font_scaler(application)
    widget.setProperty(_FONT_SCALE_PROPERTY, float(factor))
    widget.setProperty(
        _FONT_WEIGHT_PROPERTY,
        None if weight is None else int(weight),
    )
    widget.setProperty(_FONT_MONOSPACE_PROPERTY, monospace)
    widget.setFont(_scaled_application_font(widget, factor, weight, monospace=monospace))
