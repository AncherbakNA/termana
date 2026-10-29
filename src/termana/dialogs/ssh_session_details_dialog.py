"""Modeless requested settings and separately verified Embedded destination facts."""

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QResizeEvent, QTextOption
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.connection import ConnectionProfile
from termana.models.open_terminal import OpenTerminalState
from termana.models.ssh_negotiation import SshNegotiatedFacts
from termana.models.ssh_session_status import SshSessionStatus
from termana.ssh_session_details import (
    SSH_DETAILS_PRIVACY,
    SSH_DETAILS_REDACTED_PRIVACY,
    ssh_session_details_text,
)
from termana.ui_typography import scale_widget_font
from termana.widgets.clipboard_text import copy_plain_text
from termana.widgets.document_search import DocumentSearch
from termana.widgets.themed_checkbox import ThemedCheckBox
from termana.widgets.wrapping_label import WrappingLabel


class SshSessionDetailsDialog(QDialog):
    """No modal loop, automatic clipboard write, network query, or editable settings."""

    export_report_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        profile: ConnectionProfile,
        status: SshSessionStatus | None = None,
        negotiation: SshNegotiatedFacts | None = None,
    ) -> None:
        text = ssh_session_details_text(profile, status=status, negotiation=negotiation)
        super().__init__(parent)
        self._profile = profile
        self._report_revision = 0
        self._status = status
        self._negotiation = (
            None if status is not None and status.state is OpenTerminalState.ENDED else negotiation
        )
        self.setObjectName("sshSessionDetailsDialog")
        self.setWindowTitle(tr("SSH connection details"))
        self.setAccessibleName(tr("SSH session launch settings and verified destination facts"))
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(360, 280)
        self.resize(700, 650)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)
        title = QLabel(tr("SSH connection details"))
        title.setObjectName("dialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        scale_widget_font(title, 1.4, weight=QFont.Weight.Bold)
        root.addWidget(title)
        self.redact_checkbox = ThemedCheckBox(tr("Hide identifiers"))
        self.redact_checkbox.setObjectName("sshDetailsRedact")
        self.redact_checkbox.setToolTip(
            tr(
                "Hide session name, addresses, ports, usernames, identity-file path and server-key fingerprint in this report. The terminal and any previous clipboard copy are unchanged."
            )
        )
        root.addWidget(self.redact_checkbox)
        self.details_display = QPlainTextEdit()
        self.details_display.setObjectName("sshSessionDetailsText")
        self.details_display.setReadOnly(True)
        self.details_display.setTabChangesFocus(True)
        self.details_display.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.details_display.setAccessibleName(
            tr("Requested SSH settings and separate verified facts")
        )
        self.details_display.setPlainText(text)
        self.search = DocumentSearch(self.details_display, self)
        root.addWidget(self.search)
        root.addWidget(self.details_display, 1)
        self.privacy_label = WrappingLabel(tr(SSH_DETAILS_PRIVACY))
        self.privacy_label.setObjectName("mutedText")
        self.privacy_label.setTextFormat(Qt.TextFormat.PlainText)
        self.privacy_label.setWordWrap(True)
        root.addWidget(self.privacy_label)
        self.export_status = WrappingLabel("")
        self.export_status.setObjectName("sshReportExportStatus")
        self.export_status.setTextFormat(Qt.TextFormat.PlainText)
        self.export_status.hide()
        root.addWidget(self.export_status)
        self.button_box = QDialogButtonBox()
        self.copy_button = QPushButton(tr("Copy details"))
        self.copy_button.setProperty("actionRole", "secondary")
        self.copy_button.setAutoDefault(False)
        self.export_button = QPushButton(tr("Save redacted…"))
        self.export_button.setObjectName("saveRedactedSshReport")
        self.export_button.setAccessibleName(tr("Save redacted SSH report"))
        self.export_button.setProperty("actionRole", "secondary")
        self.export_button.setAutoDefault(False)
        self.export_button.setToolTip(
            tr(
                "Save a redacted UTF-8 report up to 64 KiB to a new private local file. Identifiers are always hidden, regardless of the display checkbox. Existing files are never replaced."
            )
        )
        self.close_button = QPushButton(tr("Close"))
        self.close_button.setProperty("actionRole", "primary")
        self.close_button.setDefault(True)
        self.button_box.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.button_box.addButton(self.export_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.button_box.addButton(self.close_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.button_box.rejected.connect(self.reject)
        self.copy_button.clicked.connect(self._copy_details)
        self.export_button.clicked.connect(self._request_report_export)
        self.redact_checkbox.toggled.connect(self._privacy_changed)
        root.addWidget(self.button_box)
        self.close_button.setFocus()
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(700, available.width()), min(650, max(280, available.height() - 80)))

    def set_status(self, status: SshSessionStatus) -> None:
        self.set_session_state(status, self._negotiation)

    @property
    def report_revision(self) -> int:
        """Monotonic data revision, including a state which changes then changes back."""
        return self._report_revision

    def set_export_busy(self, busy: bool) -> None:
        self.export_button.setEnabled(not busy)

    def set_export_feedback(self, message: str) -> None:
        self.export_status.setText(message)
        self.export_status.setVisible(bool(message))

    def _request_report_export(self) -> None:
        if self.isVisible() and self.export_button.isEnabled():
            # Let the user review exactly the privacy mode this action always uses.
            self.redact_checkbox.setChecked(True)
            self.export_report_requested.emit()

    def set_session_state(
        self, status: SshSessionStatus | None, negotiation: SshNegotiatedFacts | None
    ) -> None:
        if status is not None and type(status) is not SshSessionStatus:
            raise TypeError("Invalid SSH process state")
        if negotiation is not None and type(negotiation) is not SshNegotiatedFacts:
            raise TypeError("Invalid SSH negotiation snapshot")
        if status is not None and status.state is OpenTerminalState.ENDED:
            negotiation = None
        if status != self._status or negotiation != self._negotiation:
            self._status, self._negotiation = status, negotiation
            self._report_revision += 1
            self._refresh_report()

    def _privacy_changed(self, redacted: bool) -> None:
        if redacted:
            # Search text can itself contain a username/path when sharing a screenshot.
            self.search.query.clear()
        self._refresh_report()

    def _refresh_report(self) -> None:
        redacted = self.redact_checkbox.isChecked()
        self.details_display.setPlainText(
            ssh_session_details_text(
                self._profile,
                redacted=redacted,
                status=self._status,
                negotiation=self._negotiation,
            )
        )
        self.privacy_label.setText(
            tr(SSH_DETAILS_REDACTED_PRIVACY) if redacted else tr(SSH_DETAILS_PRIVACY)
        )
        self.copy_button.setText(tr("Copy report") if redacted else tr("Copy details"))
        self.copy_button.setAccessibleName(self.copy_button.text())
        self._reflow_buttons()

    def _copy_details(self) -> None:
        if self.isVisible():
            copied = copy_plain_text(self.details_display.toPlainText())
            self.copy_button.setText(tr("Copied") if copied else tr("Copy unavailable"))
            self.copy_button.setAccessibleName(
                tr("SSH details copied to clipboard")
                if copied
                else tr("Clipboard unavailable; SSH details not copied")
            )
            self._reflow_buttons()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow_buttons()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            QTimer.singleShot(0, self, self._reflow_buttons)

    def _reflow_buttons(self) -> None:
        if not hasattr(self, "button_box"):
            return
        required = sum(button.sizeHint().width() for button in self.button_box.buttons()) + 36
        self.button_box.setOrientation(
            Qt.Orientation.Vertical if required > self.width() - 48 else Qt.Orientation.Horizontal
        )
