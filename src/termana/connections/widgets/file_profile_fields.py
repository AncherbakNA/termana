"""Protocol-specific public preferences in the existing Sessions editor."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QLabel, QWidget

from termana.models.connection import ConnectionProtocol
from termana.models.file_options import DEFAULT_FILE_OPTIONS, FileOptions
from termana.ui.i18n import tr
from termana.ui.widgets.form_controls import FormComboBox, FormSpinBox


class FileProfileFields(QWidget):
    def __init__(self, parent=None, *, options=DEFAULT_FILE_OPTIONS):
        super().__init__(parent)
        self._protocol = ConnectionProtocol.SSH
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.security = FormComboBox()
        self.security.addItem(tr("FTP — unencrypted"), "plain")
        self.security.addItem(tr("FTP over TLS — explicit"), "explicit-tls")
        self.security.setCurrentIndex(self.security.findData(options.ftp_security))
        self.encoding = FormComboBox()
        for encoding in ("utf-8", "latin-1", "cp1251"):
            self.encoding.addItem(encoding.upper(), encoding)
        self.encoding.setCurrentIndex(self.encoding.findData(options.ftp_encoding))
        self.timeout = FormSpinBox()
        self.timeout.setRange(1, 600)
        self.timeout.setSuffix(tr(" s"))
        self.timeout.setValue(options.timeout)
        self.block_size = FormSpinBox()
        self.block_size.setRange(8, 65464)
        self.block_size.setValue(options.tftp_block_size)
        self.retries = FormSpinBox()
        self.retries.setRange(0, 10)
        self.retries.setValue(options.tftp_retries)
        self._rows = []
        for text, control, protocols in (
            ("Security", self.security, (ConnectionProtocol.FTP,)),
            ("Filename encoding", self.encoding, (ConnectionProtocol.FTP,)),
            ("Network timeout", self.timeout, (ConnectionProtocol.FTP, ConnectionProtocol.TFTP)),
            ("TFTP block size", self.block_size, (ConnectionProtocol.TFTP,)),
            ("TFTP retries", self.retries, (ConnectionProtocol.TFTP,)),
        ):
            label = QLabel(tr(text))
            label.setBuddy(control)
            control.setAccessibleName(tr(text))
            form.addRow(label, control)
            self._rows.append((label, control, protocols))
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(self.note)

    def set_protocol(self, protocol):
        self._protocol = protocol
        self.setVisible(protocol in (ConnectionProtocol.FTP, ConnectionProtocol.TFTP))
        for label, control, protocols in self._rows:
            label.setVisible(protocol in protocols)
            control.setVisible(protocol in protocols)
        self.note.setText(tr(
            "FTP uses a separate file connection. Unencrypted FTP sends the login and files without encryption."
        ) if protocol is ConnectionProtocol.FTP else tr(
            "TFTP transfers a known filename. The protocol cannot list folders or authenticate users."
        ))

    @property
    def selected(self):
        if self._protocol is ConnectionProtocol.FTP:
            return FileOptions(
                ftp_security=self.security.currentData(), ftp_encoding=self.encoding.currentData(),
                timeout=self.timeout.value(),
            )
        if self._protocol is ConnectionProtocol.TFTP:
            return FileOptions(
                timeout=self.timeout.value(), tftp_block_size=self.block_size.value(),
                tftp_retries=self.retries.value(),
            )
        return FileOptions()
