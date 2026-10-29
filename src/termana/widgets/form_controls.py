"""Form inputs that leave wheel gestures to their containing scroll area."""

from PySide6.QtCore import QEvent, QMargins, Qt, QTimer
from PySide6.QtGui import QPalette, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionFrame,
    QStyleOptionSpinBox,
    QStylePainter,
)

from termana.i18n import tr
from termana.theme import FORM_FIELD_HORIZONTAL_PADDING
from termana.widgets.form_indicators import paint_form_arrow


class FormComboBox(QComboBox):
    """Change choices explicitly with the popup or keyboard, never by scrolling past."""

    def __init__(self, parent=None, **properties):
        self._manual_edit = False
        self._edit_original = ""
        super().__init__(parent, **properties)

    def setEditable(self, editable: bool) -> None:
        super().setEditable(editable)
        if editable:
            self.lineEdit().installEventFilter(self)
            self._finish_custom_edit()
            self.lineEdit().setToolTip(tr("Click to choose. Use Custom… or F2 to enter a value."))

    def custom_value_button(self, parent=None):
        button = QPushButton(tr("Custom…"), parent)
        button.setObjectName(self.objectName() + "CustomButton")
        button.setAccessibleName(tr("Enter a custom value for {p0}", p0=self.accessibleName()))
        button.setToolTip(tr("Enter a value manually (F2)"))
        button.setProperty("actionRole", "secondary")
        button.setAutoDefault(False)
        button.clicked.connect(self.begin_custom_edit)
        return button

    def begin_custom_edit(self, _checked=False):
        if not self.isEditable() or not self.isEnabled():
            return
        if not self._manual_edit:
            self._edit_original = self.currentText()
        self._manual_edit = True
        self.lineEdit().setReadOnly(False)
        self.lineEdit().setCursor(Qt.CursorShape.IBeamCursor)
        self.lineEdit().setFocus(Qt.FocusReason.OtherFocusReason)
        self.lineEdit().selectAll()

    def _finish_custom_edit(self):
        self._manual_edit = False
        if self.lineEdit() is not None:
            self.lineEdit().setReadOnly(True)
            self.lineEdit().setCursor(Qt.CursorShape.ArrowCursor)

    def showPopup(self):
        self._finish_custom_edit()
        super().showPopup()

    def mousePressEvent(self, event):
        if (self.isEditable() and not self._manual_edit
                and event.button() == Qt.MouseButton.LeftButton):
            self.showPopup()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if self._handle_custom_edit_key(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def _handle_custom_edit_key(self, event):
        """Consume editor actions on either native combo or line-editor routing."""
        if not self.isEditable():
            return False
        if event.key() == Qt.Key.Key_F2:
            self.begin_custom_edit()
            return True
        if self._manual_edit and event.key() in (
            Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape
        ):
            if event.key() == Qt.Key.Key_Escape:
                self.setEditText(self._edit_original)
            self._finish_custom_edit()
            return True
        return False

    def eventFilter(self, watched, event):
        if self.isEditable() and watched is self.lineEdit():
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
                and not self._manual_edit
            ):
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.showPopup()
                return True
            if event.type() == QEvent.Type.FocusOut:
                self._finish_custom_edit()
            elif event.type() == QEvent.Type.KeyPress:
                if self._handle_custom_edit_key(event):
                    return True
                if (
                    not self._manual_edit
                    and event.text()
                    and event.text().isprintable()
                    and not event.modifiers()
                    & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
                ):
                    self.begin_custom_edit()  # Keyboard entry remains accessible.
        return super().eventFilter(watched, event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Keep form scrolling separate from selecting a different value."""
        event.ignore()

    def paintEvent(self, event) -> None:
        if (
            hasattr(self, "labelDrawingMode")
            and self.labelDrawingMode() == QComboBox.LabelDrawingMode.UseDelegate
        ):
            super().paintEvent(event)  # Preserve a caller's custom delegate path.
            return
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        arrow = QStyle.SubControl.SC_ComboBoxArrow
        original = option.subControls
        option.subControls = QStyle.SubControl(original.value & ~arrow.value)
        painter = QStylePainter(self)
        try:
            painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
            option.subControls = original
            paint_form_arrow(
                self,
                painter,
                option,
                QStyle.ComplexControl.CC_ComboBox,
                arrow,
                enabled=self.isEnabled(),
            )
            if self.currentIndex() < 0 and self.placeholderText():
                option.currentText = self.placeholderText()
                option.palette.setColor(
                    QPalette.ColorRole.ButtonText,
                    option.palette.color(QPalette.ColorRole.PlaceholderText),
                )
            painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
        finally:
            painter.end()


class ThemedSpinBox(QSpinBox):
    """Readable numeric fields, retaining native input, stepping and wheel policy."""

    def __init__(self, parent=None, **properties) -> None:
        self._inset_ready = False
        self._inset_pending = False
        self._inset_added = QMargins()
        self._editor_overhead = 0
        self._last_editor_margins = None
        super().__init__(parent, **properties)
        self._inset_ready = True

    def sizeHint(self):
        result = super().sizeHint()
        # Native Windows QSpinBox does not include the nested QLineEdit's QSS
        # padding in its size hint. Measure the actual editor area after layout,
        # including its buttons/frame/margins, instead of guessing a pixel width.
        texts = [self.prefix() + self.textFromValue(value) + self.suffix()
                 for value in (self.minimum(), self.maximum())]
        texts.append(self.specialValueText())
        numeric_width = max(self.lineEdit().fontMetrics().horizontalAdvance(text) for text in texts)
        result.setWidth(max(result.width(), numeric_width + self._editor_overhead + 2))
        return result

    def minimumSizeHint(self):
        return self.sizeHint()

    def _schedule_editor_inset(self) -> None:
        if self._inset_ready and not self._inset_pending:
            self._inset_pending = True
            QTimer.singleShot(0, self, self._update_editor_inset)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_editor_inset()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_editor_inset()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.StyleChange,
            QEvent.Type.FontChange,
            QEvent.Type.LayoutDirectionChange,
        ):
            self._schedule_editor_inset()

    def _update_editor_inset(self) -> None:
        self._inset_pending = False
        editor = self.lineEdit()
        current = editor.textMargins()
        # Remove only our previous contribution. Respect new caller/Qt margins.
        base = current - self._inset_added if current == self._last_editor_margins else current
        option = QStyleOptionFrame()
        editor.initStyleOption(option)
        content = editor.style().subElementRect(
            QStyle.SubElement.SE_LineEditContents, option, editor
        )
        styled = bool(self.styleSheet() or QApplication.instance().styleSheet())
        minimum = FORM_FIELD_HORIZONTAL_PADDING if styled else 0
        left_gap = editor.x() + content.left() + base.left()
        right_gap = self.width() - editor.x() - content.right() - 1 + base.right()
        self._inset_added = QMargins(max(0, minimum - left_gap), 0, max(0, minimum - right_gap), 0)
        desired = base + self._inset_added
        overhead = self.width() - content.width() + desired.left() + desired.right()
        overhead_changed = overhead != self._editor_overhead
        self._editor_overhead = overhead
        self._last_editor_margins = QMargins(desired)
        if current != desired:
            editor.setTextMargins(desired)
        if current != desired or overhead_changed:
            self.updateGeometry()

    def paintEvent(self, event) -> None:
        if self.buttonSymbols() != QAbstractSpinBox.ButtonSymbols.UpDownArrows:
            super().paintEvent(event)
            return
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        # Some Windows styles expose both step flags in the paint option even
        # when the widget itself correctly disallows one at the value limit.
        enabled_steps = (
            QAbstractSpinBox.StepEnabledFlag.StepNone
            if self.isReadOnly() or not self.isEnabled()
            else self.stepEnabled()
        )
        option.stepEnabled = enabled_steps
        up, down = QStyle.SubControl.SC_SpinBoxUp, QStyle.SubControl.SC_SpinBoxDown
        original = option.subControls
        option.subControls = QStyle.SubControl(original.value & ~(up | down).value)
        painter = QStylePainter(self)
        try:
            painter.drawComplexControl(QStyle.ComplexControl.CC_SpinBox, option)
            option.subControls = original
            for part, flag, is_up in (
                (up, QAbstractSpinBox.StepEnabledFlag.StepUpEnabled, True),
                (down, QAbstractSpinBox.StepEnabledFlag.StepDownEnabled, False),
            ):
                paint_form_arrow(
                    self,
                    painter,
                    option,
                    QStyle.ComplexControl.CC_SpinBox,
                    part,
                    up=is_up,
                    enabled=(enabled_steps & flag).value != 0,
                )
        finally:
            painter.end()


class FormSpinBox(ThemedSpinBox):
    """Keep typed values and step buttons without accidental wheel edits in forms."""

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Let the containing form handle both mouse-wheel and trackpad scrolling."""
        event.ignore()
