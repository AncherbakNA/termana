"""Edit a display-only terminal tab label, never the saved connection."""

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAccessible, QAccessibleEvent, QFont, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr, validation_text
from termana.terminal.tab_title import (
    MAX_CUSTOM_TAB_TITLE_CHARACTERS,
    validate_custom_tab_title,
)
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.wrapping_label import WrappingLabel


class TerminalTabTitleDialog(QDialog):
    """Keep validation local and return a label only after explicit acceptance."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        session_name: str,
        custom_title: str | None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("terminalTabTitleDialog")
        self.setWindowTitle(tr("Rename tab"))
        self.setModal(True)
        self.setMinimumWidth(440)
        self.resize(540, 370)
        self._result_title: str | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("settingsScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("settingsScrollContent")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 22, 24, 14)
        layout.setSpacing(12)
        title = self._label(tr("Rename tab"), "dialogTitle")
        scale_widget_font(title, 1.7, weight=QFont.Weight.Bold)
        layout.addWidget(title)
        self.session_label = QLineEdit(session_name)
        self.session_label.setReadOnly(True)
        self.session_label.setAccessibleName(tr("Original session name"))
        self.session_label.setToolTip(plain_text_tooltip(session_name))
        self.session_label.setCursorPosition(0)
        self.session_label.setMinimumWidth(0)
        layout.addWidget(self.session_label)
        layout.addWidget(
            self._label(
                tr(
                    "Give this tab a name that the server cannot change. A new connection uses its automatic title."
                ),
                "mutedText",
            )
        )
        self.name_input = QLineEdit()
        self.name_input.setObjectName("terminalTabTitleInput")
        self.name_input.setAccessibleName(tr("Terminal tab name"))
        # The validator counts NFC code points, not Qt's UTF-16 units. Keep the
        # normal bounded editor capacity so decomposed Unicode is not truncated.
        self.name_input.setAccessibleDescription(
            tr(
                "Up to {p0} characters after Unicode normalization.",
                p0=f"{MAX_CUSTOM_TAB_TITLE_CHARACTERS}",
            )
        )
        self.name_input.setPlaceholderText(tr("Production · Logs"))
        name_label = self._label(tr("Tab name"), "settingsSectionTitle")
        name_label.setBuddy(self.name_input)
        layout.addWidget(name_label)
        layout.addWidget(self.name_input)
        self.error_label = self._label("", "settingsError")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        layout.addStretch()
        self.scroll_area.setWidget(body)
        root.addWidget(self.scroll_area, 1)

        self.footer = QWidget()
        self.footer.setObjectName("settingsDialogFooter")
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(24, 12, 24, 20)
        buttons = QDialogButtonBox()
        self.button_box = buttons
        self.reset_button = QPushButton(tr("Use automatic title"))
        self.reset_button.setObjectName("resetTerminalTabTitleButton")
        self.reset_button.setProperty("actionRole", "secondary")
        self.reset_button.setEnabled(custom_title is not None)
        self.reset_button.setAutoDefault(False)
        buttons.addButton(self.reset_button, QDialogButtonBox.ButtonRole.ResetRole)
        self.save_button = QPushButton(tr("Rename"))
        self.save_button.setObjectName("saveTerminalTabTitleButton")
        self.save_button.setProperty("actionRole", "primary")
        self.save_button.setDefault(True)
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_button = QPushButton(tr("Cancel"))
        self.cancel_button.setProperty("actionRole", "secondary")
        self.cancel_button.setAutoDefault(False)
        buttons.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        footer_layout.addWidget(buttons)
        root.addWidget(self.footer)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.reset_button.clicked.connect(self._reset)
        self.name_input.textChanged.connect(self._validate)
        self.name_input.setText(custom_title if custom_title is not None else session_name)
        self._validate()
        self.name_input.selectAll()
        self.name_input.setFocus()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow_buttons()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self, self._reveal_input)

    def _reveal_input(self) -> None:
        if self.isVisible():
            self.scroll_area.ensureWidgetVisible(self.name_input)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            QTimer.singleShot(0, self, self._reflow_buttons)

    def _reflow_buttons(self) -> None:
        if not hasattr(self, "button_box"):
            return
        buttons = (self.reset_button, self.save_button, self.cancel_button)
        required_width = sum(button.sizeHint().width() for button in buttons) + 24
        self.button_box.setOrientation(
            Qt.Orientation.Vertical
            if required_width > self.width() - 48
            else Qt.Orientation.Horizontal
        )

    @staticmethod
    def _label(text: str, name: str) -> QLabel:
        label = WrappingLabel(text)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    @property
    def tab_title(self) -> str | None:
        if self.result() != QDialog.DialogCode.Accepted:
            raise RuntimeError("Tab title has not been accepted")
        return self._result_title

    def _validate(self) -> str | None:
        try:
            title = validate_custom_tab_title(self.name_input.text())
        except (TypeError, ValueError) as error:
            message = validation_text(str(error))
            changed = message != self.error_label.text()
            self.error_label.setText(message)
            self.error_label.setAccessibleName(message)
            self.error_label.show()
            self.save_button.setEnabled(False)
            if changed and self.isVisible():
                QAccessible.updateAccessibility(
                    QAccessibleEvent(self.error_label, QAccessible.Event.Alert)
                )
                QTimer.singleShot(0, self, self._reveal_error)
            return None
        self.error_label.clear()
        self.error_label.setAccessibleName("")
        self.error_label.hide()
        self.save_button.setEnabled(True)
        return title

    def _reveal_error(self) -> None:
        if self.error_label.isVisible():
            self.scroll_area.ensureWidgetVisible(self.error_label)

    def accept(self) -> None:
        title = self._validate()
        if title is None:
            self.name_input.setFocus()
            return
        self._result_title = title
        super().accept()

    def _reset(self) -> None:
        if not self.reset_button.isEnabled():
            return
        self._result_title = None
        super().accept()
