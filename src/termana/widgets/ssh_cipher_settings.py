"""Session-only cipher choice discovered off the UI thread from system OpenSSH."""

from collections.abc import Callable
from uuid import UUID, uuid4

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from termana.credentials.background_operation import BackgroundOperation
from termana.i18n import tr
from termana.models.settings import SshEngine
from termana.models.ssh_cipher import validate_ssh_cipher
from termana.terminal.ssh_algorithm_discovery import discover_engine_ciphers
from termana.terminal.ssh_ciphers import discover_ssh_ciphers, is_legacy_ssh_cipher
from termana.ui_text import plain_text_tooltip
from termana.widgets.form_controls import FormComboBox
from termana.widgets.themed_checkbox import ThemedCheckBox
from termana.widgets.wrapping_label import WrappingLabel


class SshCipherSettings(QWidget):
    """Retain saved unavailable choices; never silently replace them with defaults."""

    changed = Signal()
    edited = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        ssh_cipher: str | None = None,
        discover: Callable[[], tuple[str, ...]] | None = None,
        operation_starter: Callable[[BackgroundOperation], None] | None = None,
        engine: SshEngine = SshEngine.SYSTEM,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sshCipherSettings")
        self._initial_cipher = validate_ssh_cipher(ssh_cipher)
        self._engine = engine
        self._discover = discover or (
            discover_ssh_ciphers
            if engine is SshEngine.SYSTEM
            else lambda: discover_engine_ciphers(engine)
        )
        self._start = operation_starter or QThreadPool.globalInstance().start
        self._available: tuple[str, ...] | None = None
        self._operation: BackgroundOperation | None = None
        self._operation_id: UUID | None = None
        self._attempted = False
        self._load_failed = False
        self._automatic_load = False
        self._auto_retry_used = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.cipher_input = FormComboBox()
        self.cipher_input.setObjectName("sshCipherInput")
        self.cipher_input.setAccessibleName(tr("SSH encryption cipher"))
        self.cipher_input.setSizeAdjustPolicy(
            FormComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.cipher_input.setMinimumContentsLength(1)
        self.cipher_input.setMinimumWidth(0)
        row.addWidget(self.cipher_input, 1)
        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setObjectName("refreshSshCiphersButton")
        self.refresh_button.setProperty("actionRole", "secondary")
        self.refresh_button.setAccessibleName(tr("Refresh supported SSH ciphers"))
        self.refresh_button.setToolTip(tr("Query local engine capabilities; no server connection"))
        self.refresh_button.setAutoDefault(False)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.notice = WrappingLabel()
        self.notice.setObjectName("mutedText")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.setMinimumWidth(0)
        layout.addWidget(self.notice)
        self.legacy_consent = ThemedCheckBox(tr("Allow legacy cipher"))
        # Cocoa's checkbox layout-item margins otherwise inset the entire
        # encryption column, even when this optional consent is hidden.
        self.legacy_consent.setAttribute(Qt.WidgetAttribute.WA_LayoutUsesWidgetRect)
        self.legacy_consent.setObjectName("allowLegacySshCipher")
        self.legacy_consent.setAccessibleDescription(
            tr("Enable only when a legacy server requires this less secure cipher.")
        )
        layout.addWidget(self.legacy_consent)
        self._populate_choices(ssh_cipher)
        self.cipher_input.currentIndexChanged.connect(self.edited)
        self.cipher_input.currentIndexChanged.connect(self._selection_changed)
        self.legacy_consent.toggled.connect(self.edited)
        self.legacy_consent.toggled.connect(self.changed)
        self.refresh_button.clicked.connect(self.refresh)
        self._selection_changed()

    @property
    def selected_cipher(self) -> str | None:
        return validate_ssh_cipher(self.cipher_input.currentData())

    @property
    def selection_error(self) -> str | None:
        try:
            selected = self.selected_cipher
        except (TypeError, ValueError):
            return tr("Choose a valid SSH cipher.")
        if selected is None:
            return None
        if selected != self._initial_cipher and (
            self._available is None or selected not in self._available
        ):
            return tr("Choose a cipher supported by the selected SSH engine.")
        if is_legacy_ssh_cipher(selected) and not self.legacy_consent.isChecked():
            return tr("Confirm legacy cipher use, or choose Automatic or a modern cipher.")
        return None

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._attempted:
            self.refresh(automatic=True)

    @Slot()
    def refresh(self, *, automatic: bool = False) -> None:
        if self._operation is not None:
            return
        self._attempted = True
        self._load_failed = False
        self._automatic_load = automatic
        operation_id = uuid4()
        operation = BackgroundOperation(operation_id, self._discover)
        self._operation_id = operation_id
        self._operation = operation
        operation.signals.succeeded.connect(self._loaded)
        operation.signals.failed.connect(self._failed)
        self.refresh_button.setEnabled(False)
        self._update_notice()
        try:
            self._start(operation)
        except Exception:
            self._failed(operation_id)

    @Slot(object, object)
    def _loaded(self, operation_id: UUID, result: object) -> None:
        if operation_id != self._operation_id:
            return
        try:
            if type(result) is not tuple or not 1 <= len(result) <= 128:
                raise ValueError("Invalid cipher discovery result")
            if any(validate_ssh_cipher(name) is None for name in result):
                raise ValueError("Invalid cipher discovery result")
            if len(set(result)) != len(result):
                raise ValueError("Duplicate cipher discovery result")
        except (TypeError, ValueError):
            self._failed(operation_id)
            return
        selected = self.selected_cipher
        self._available = result
        self._operation_id = None
        self._operation = None
        self.refresh_button.setEnabled(True)
        self._populate_choices(selected)
        self._update_notice()
        self.changed.emit()

    @Slot(object)
    def _failed(self, operation_id: UUID) -> None:
        if operation_id != self._operation_id:
            return
        self._operation_id = None
        self._operation = None
        self._load_failed = True
        self.refresh_button.setEnabled(True)
        self._update_notice()
        self.changed.emit()
        if self._automatic_load and not self._auto_retry_used:
            self._auto_retry_used = True
            QTimer.singleShot(0, self, self._retry_initial_load)

    def _retry_initial_load(self) -> None:
        # Only the failed initial LOCAL capability read is retried once. Never
        # open a connection, change choices, or repeat after the owner is hidden.
        if self.isVisible() and self._load_failed and self._operation is None:
            self.refresh()

    def _populate_choices(self, selected: str | None) -> None:
        blocked = self.cipher_input.blockSignals(True)
        try:
            self.cipher_input.clear()
            self.cipher_input.addItem(
                tr("Automatic (Embedded SSH)")
                if self._engine is SshEngine.EMBEDDED
                else tr("Automatic (OpenSSH / config)"),
                None,
            )
            available = self._available or ()
            # Modern options precede compatibility choices; neither group is
            # selected automatically and the literal algorithm is retained.
            for name in sorted(available, key=lambda name: (is_legacy_ssh_cipher(name), name)):
                label = tr("{p0} (legacy)", p0=f"{name}") if is_legacy_ssh_cipher(name) else name
                self.cipher_input.addItem(label, name)
                self.cipher_input.setItemData(
                    self.cipher_input.count() - 1,
                    plain_text_tooltip(label),
                    Qt.ItemDataRole.ToolTipRole,
                )
            if selected is not None and selected not in available:
                label = (
                    tr("{p0} (unavailable)", p0=f"{selected}")
                    if self._available is not None
                    else tr("{p0} (saved)", p0=f"{selected}")
                )
                self.cipher_input.addItem(label, selected)
            self.cipher_input.setCurrentIndex(max(0, self.cipher_input.findData(selected)))
        finally:
            self.cipher_input.blockSignals(blocked)

    def _selection_changed(self) -> None:
        try:
            selected = self.selected_cipher
        except (TypeError, ValueError):
            # Malformed combo metadata must fail closed at the form boundary,
            # not escape a Qt signal or retain previous legacy consent.
            self.legacy_consent.hide()
            self.legacy_consent.setChecked(False)
            self._update_notice()
            self.changed.emit()
            return
        legacy = selected is not None and is_legacy_ssh_cipher(selected)
        self.legacy_consent.setVisible(legacy)
        self.legacy_consent.setChecked(legacy and selected == self._initial_cipher)
        self._update_notice()
        self.changed.emit()

    def _update_notice(self) -> None:
        try:
            selected = self.selected_cipher
        except (TypeError, ValueError):
            message = tr("Choose a valid SSH cipher.")
            self.notice.setText(message)
            self.notice.setAccessibleName(message)
            self.cipher_input.setToolTip(plain_text_tooltip(message))
            return
        if self._operation is not None:
            message = tr("Loading available ciphers…")
        elif self._load_failed:
            message = tr(
                "Could not load ciphers. Click Refresh to retry; your choice is unchanged."
            )
        elif selected is None:
            message = tr("Automatic lets the client and server choose a shared cipher.")
        elif self._available is not None and selected not in self._available:
            message = tr(
                "Cipher unavailable here. Choose another or Automatic."
            )
        elif is_legacy_ssh_cipher(selected):
            message = tr(
                "Legacy cipher: less secure. Only this cipher will be offered."
            )
        else:
            message = tr(
                "Only this cipher will be offered; the server must support it."
            )
        self.notice.setText(message)
        self.notice.setAccessibleName(message)
        self.cipher_input.setToolTip(plain_text_tooltip(selected or message))
