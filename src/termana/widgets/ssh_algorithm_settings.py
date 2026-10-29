"""Full-width, refreshable ordered negotiation choices with safe defaults."""

from uuid import uuid4

from PySide6.QtCore import Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from termana.credentials.background_operation import BackgroundOperation
from termana.i18n import tr
from termana.models.settings import SshEngine
from termana.models.ssh_algorithms import SshAlgorithms
from termana.terminal.ssh_algorithm_discovery import (
    discover_ssh_algorithms,
    validate_algorithm_list,
)
from termana.ui_text import plain_text_tooltip
from termana.widgets.form_controls import FormComboBox
from termana.widgets.wrapping_label import WrappingLabel


class AlgorithmChoice(QWidget):
    changed = Signal()
    edited = Signal()

    def __init__(self, kind, label, selected, engine, *, discover=None, start=None, parent=None):
        super().__init__(parent)
        self._automatic = (
            tr("Automatic (Embedded SSH)")
            if engine is SshEngine.EMBEDDED
            else tr("Automatic (OpenSSH / config)")
        )
        self._discover = discover or (lambda: discover_ssh_algorithms(kind, engine))
        self._start = start or QThreadPool.globalInstance().start
        self._operation = None
        self._operation_id = None
        self._attempted = False
        self._available = ()
        self._engine_label = "Embedded SSH" if engine is SshEngine.EMBEDDED else "OpenSSH"
        self._default_hint = (
            "Establishes session keys. Automatic chooses a shared method."
            if kind == "kex" else "Checks server identity. Automatic chooses a shared key type."
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        title = WrappingLabel(tr(label))
        title.setWordWrap(True)
        title.setMinimumWidth(0)
        heading = QHBoxLayout()
        heading.addWidget(title, 1)
        layout.addLayout(heading)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.input = FormComboBox(self)
        self.input.setObjectName(
            "sshKexAlgorithmsInput" if kind == "kex" else "sshHostKeyTypesInput"
        )
        self.input.setAccessibleName(tr(label))
        self.input.setEditable(True)
        self.input.setInsertPolicy(FormComboBox.InsertPolicy.NoInsert)
        self.input.setCompleter(None)
        self.input.lineEdit().setMaxLength(4096)
        self.input.setMinimumWidth(0)
        self.input.setMinimumContentsLength(1)
        self.input.setSizeAdjustPolicy(
            FormComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        title.setBuddy(self.input)
        row.addWidget(self.input, 1)
        self.custom_button = self.input.custom_value_button(self)
        heading.addWidget(self.custom_button)
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setProperty("actionRole", "secondary")
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.setAccessibleName(tr("Refresh {p0}", p0=label))
        self.refresh_button.setToolTip(tr("Query local engine capabilities; no server connection"))
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.notice = WrappingLabel()
        self.notice.setObjectName("mutedText")
        layout.addWidget(self.notice)
        self._populate(",".join(selected) or self._automatic)
        self.input.currentTextChanged.connect(self._edited)
        self.input.currentTextChanged.connect(self.edited)
        self.refresh_button.clicked.connect(self.refresh)
        self._edited()

    @property
    def selected(self):
        text = self.input.currentText().strip()
        return (
            () if not text or text == self._automatic else tuple(n.strip() for n in text.split(","))
        )

    def _populate(self, text):
        blocked = self.input.blockSignals(True)
        try:
            self.input.clear()
            self.input.addItem(self._automatic)
            for name in self._available:
                self.input.addItem(name)
                self.input.setItemData(
                    self.input.count() - 1, plain_text_tooltip(name), Qt.ItemDataRole.ToolTipRole
                )
            self.input.setEditText(text)
        finally:
            self.input.blockSignals(blocked)

    @Slot()
    def _edited(self):
        self.input.setToolTip(plain_text_tooltip(self.input.currentText()))
        try:
            selected = self.selected
            SshAlgorithms(kex=selected)
        except ValueError:
            message = tr("Enter unique algorithm names separated by commas, in preferred order.")
        else:
            unavailable = [n for n in selected if n not in self._available]
            if self._operation is not None:
                message = tr("Checking local {p0} capabilities…", p0=self._engine_label)
            elif selected and self._attempted and unavailable:
                message = tr(
                    "Some selected algorithms are unavailable in this engine. Your list is kept unchanged; there is no silent fallback."
                )
            else:
                message = (tr("Only selected algorithms are offered, in the listed order.")
                           if selected else tr(self._default_hint))
        self.notice.setText(message)
        self.changed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._attempted:
            self.refresh()

    @Slot()
    def refresh(self):
        if self._operation is not None:
            return
        self._attempted = True
        self._operation_id = uuid4()
        operation = BackgroundOperation(self._operation_id, self._discover)
        self._operation = operation
        operation.signals.succeeded.connect(self._loaded)
        operation.signals.failed.connect(self._failed)
        self.refresh_button.setEnabled(False)
        self._edited()
        try:
            self._start(operation)
        except Exception:
            self._failed(self._operation_id)

    @Slot(object, object)
    def _loaded(self, operation_id, result):
        if operation_id != self._operation_id:
            return
        try:
            available = validate_algorithm_list(result)
        except (ValueError, TypeError):
            self._failed(operation_id)
            return
        text = self.input.currentText()
        self._available = available
        self._operation = self._operation_id = None
        self.refresh_button.setEnabled(True)
        self._populate(text)
        self._edited()

    @Slot(object)
    def _failed(self, operation_id):
        if operation_id != self._operation_id:
            return
        self._operation = self._operation_id = None
        self.refresh_button.setEnabled(True)
        self.notice.setText(
            tr("Could not read local algorithms. Retry with Refresh; your selection is unchanged.")
        )
        self.changed.emit()


class SshAlgorithmSettings(QWidget):
    changed = Signal()
    edited = Signal()

    def __init__(self, value: SshAlgorithms, parent=None, *, engine=SshEngine.SYSTEM):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)
        self.kex_choice = AlgorithmChoice("kex", "Key exchange algorithms", value.kex, engine)
        self.host_keys_choice = AlgorithmChoice(
            "host_keys", "Host key types", value.host_keys, engine
        )
        self.kex, self.host_keys = self.kex_choice.input, self.host_keys_choice.input
        for choice in (self.kex_choice, self.host_keys_choice):
            layout.addWidget(choice)
            choice.changed.connect(self.changed)
            choice.edited.connect(self.edited)
        label = QLabel(tr("SSH protocol version"))
        layout.addWidget(label)
        self.protocol = FormComboBox()
        self.protocol.setObjectName("sshProtocolVersionInput")
        self.protocol.addItem(tr("Automatic (SSH2)"), "auto")
        self.protocol.addItem(tr("SSH2 only"), "2")
        self.protocol.addItem(tr("SSH1 only (insecure)"), "1")
        self.protocol.setMinimumWidth(0)
        self.protocol.setMinimumContentsLength(1)
        self.protocol.setSizeAdjustPolicy(FormComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.protocol.setCurrentIndex(self.protocol.findData(value.protocol))
        label.setBuddy(self.protocol)
        self.protocol.setAccessibleName(tr("SSH protocol version"))
        layout.addWidget(self.protocol)
        self.protocol.currentIndexChanged.connect(self.changed)
        self.protocol.currentIndexChanged.connect(self.edited)
        notice = WrappingLabel(
            tr(
                "Automatic uses SSH2. SSH1 is insecure legacy mode: Direct only, no Files or tunnels."
            )
        )
        notice.setObjectName("mutedText")
        layout.addWidget(notice)

    @property
    def selected(self):
        return SshAlgorithms(
            self.kex_choice.selected, self.host_keys_choice.selected, self.protocol.currentData()
        )

    @property
    def selection_error(self):
        try:
            _ = self.selected
        except ValueError as error:
            return str(error)
        return None
