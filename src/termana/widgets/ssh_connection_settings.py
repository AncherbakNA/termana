"""Explicit, per-profile SSH timeouts without changing system defaults silently."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from termana.i18n import tr, validation_text
from termana.models.ssh_options import (
    MAX_SSH_CONNECT_TIMEOUT,
    MAX_SSH_SERVER_ALIVE_COUNT,
    MAX_SSH_SERVER_ALIVE_INTERVAL,
    SshConnectionOptions,
)
from termana.widgets.form_controls import FormComboBox, FormSpinBox
from termana.widgets.wrapping_label import WrappingLabel


class SshConnectionSettings(QWidget):
    """Keep automatic, disabled, and custom keepalive choices distinct."""

    changed = Signal()
    expanded_changed = Signal(bool)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        ssh_options: SshConnectionOptions | None = None,
    ) -> None:
        super().__init__(parent)
        options = SshConnectionOptions() if ssh_options is None else ssh_options
        if type(options) is not SshConnectionOptions:
            raise ValueError("Expected SSH connection options")
        self.setObjectName("sshConnectionSettings")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toggle = QToolButton(self)
        self.toggle.setObjectName("sshConnectionSettingsToggle")
        self.toggle.setText(tr("Connection behavior"))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(tr("SSH connection behavior"))
        self.toggle.setAccessibleDescription(tr("Expand timeout and keepalive settings"))
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.summary = self._notice()
        layout.addWidget(self.summary)

        self.details = QWidget(self)
        form = QFormLayout(self.details)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.timeout_mode = self._choice(tr("SSH connection timeout mode"))
        self.timeout_mode.addItem(tr("Use SSH config"), "config")
        self.timeout_mode.addItem(tr("Custom timeout"), "custom")
        self._add_row(form, tr("Connection timeout"), self.timeout_mode)
        self.timeout_input = self._number(
            tr("SSH connection timeout in seconds"), MAX_SSH_CONNECT_TIMEOUT
        )
        self.timeout_input.setSuffix(tr(" s"))
        self.timeout_input.setValue(options.connect_timeout or 15)
        self.timeout_label = self._add_row(form, tr("Timeout after"), self.timeout_input)
        self.timeout_notice = self._notice()
        self.timeout_notice.setText(
            tr("Limits the initial connection and SSH handshake, not password entry.")
        )
        form.addRow(self.timeout_notice)

        self.keepalive_mode = self._choice(tr("SSH keepalive mode"))
        self.keepalive_mode.addItem(tr("Use SSH config"), "config")
        self.keepalive_mode.addItem(tr("Disabled"), "off")
        self.keepalive_mode.addItem(tr("Send keepalives"), "custom")
        self._add_row(form, tr("Keepalive"), self.keepalive_mode)
        self.interval_input = self._number(
            tr("SSH keepalive interval in seconds"), MAX_SSH_SERVER_ALIVE_INTERVAL
        )
        self.interval_input.setSuffix(tr(" s"))
        self.interval_input.setValue(options.server_alive_interval or 60)
        self.interval_label = self._add_row(form, tr("Interval"), self.interval_input)
        self.count_input = self._number(
            tr("Maximum unanswered SSH keepalives"), MAX_SSH_SERVER_ALIVE_COUNT
        )
        self.count_input.setValue(options.server_alive_count_max or 3)
        self.count_label = self._add_row(form, tr("Unanswered probes"), self.count_input)
        self.keepalive_notice = self._notice()
        form.addRow(self.keepalive_notice)
        layout.addWidget(self.details)

        self.timeout_mode.setCurrentIndex(0 if options.connect_timeout is None else 1)
        self.keepalive_mode.setCurrentIndex(
            0
            if options.server_alive_interval is None
            else 1
            if options.server_alive_interval == 0
            else 2
        )
        for control in (self.timeout_mode, self.keepalive_mode):
            control.currentIndexChanged.connect(self._update_state)
        for control in (self.timeout_input, self.interval_input, self.count_input):
            control.valueChanged.connect(self._update_state)
        self.toggle.toggled.connect(self._set_expanded)
        self.toggle.setChecked(not options.is_default)
        self._set_expanded(self.toggle.isChecked())
        self._update_state()

    @staticmethod
    def _notice() -> QLabel:
        label = WrappingLabel()
        label.setObjectName("mutedText")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        return label

    @staticmethod
    def _choice(name: str) -> FormComboBox:
        control = FormComboBox()
        control.setAccessibleName(name)
        # These labels are fixed and short. Keep the full choice readable;
        # QFormLayout wraps the row when large fonts need more horizontal space.
        control.setSizeAdjustPolicy(FormComboBox.SizeAdjustPolicy.AdjustToContents)
        return control

    @staticmethod
    def _number(name: str, maximum: int) -> FormSpinBox:
        control = FormSpinBox()
        control.setAccessibleName(name)
        control.setRange(1, maximum)
        return control

    @staticmethod
    def _add_row(form: QFormLayout, text: str, control: QWidget) -> QLabel:
        label = QLabel(text)
        label.setBuddy(control)
        form.addRow(label, control)
        return label

    @property
    def selected_options(self) -> SshConnectionOptions:
        timeout_mode = self.timeout_mode.currentData()
        keepalive_mode = self.keepalive_mode.currentData()
        if timeout_mode not in ("config", "custom") or keepalive_mode not in (
            "config",
            "off",
            "custom",
        ):
            raise ValueError(tr("Choose valid SSH connection behavior settings."))
        return SshConnectionOptions(
            connect_timeout=self.timeout_input.value() if timeout_mode == "custom" else None,
            server_alive_interval=(
                self.interval_input.value()
                if keepalive_mode == "custom"
                else 0
                if keepalive_mode == "off"
                else None
            ),
            server_alive_count_max=self.count_input.value() if keepalive_mode == "custom" else None,
        )

    @property
    def selection_error(self) -> str | None:
        try:
            _ = self.selected_options
        except ValueError as error:
            return validation_text(str(error))
        return None

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details.setVisible(expanded)
        self.expanded_changed.emit(expanded)

    def _update_state(self) -> None:
        timeout_custom = self.timeout_mode.currentData() == "custom"
        keepalive_custom = self.keepalive_mode.currentData() == "custom"
        for control in (self.timeout_label, self.timeout_input):
            control.setVisible(timeout_custom)
            control.setEnabled(timeout_custom)
        for control in (
            self.interval_label,
            self.interval_input,
            self.count_label,
            self.count_input,
        ):
            control.setVisible(keepalive_custom)
            control.setEnabled(keepalive_custom)
        if keepalive_custom:
            seconds = self.interval_input.value() * self.count_input.value()
            self.keepalive_notice.setText(
                tr(
                    "Checks quiet connections. Disconnects after about {p0} s without a reply. No automatic reconnect.",
                    p0=f"{seconds:,}",
                )
            )
        elif self.keepalive_mode.currentData() == "off":
            self.keepalive_notice.setText(
                tr(
                    "No SSH probes; a lost connection may keep waiting. TCP keepalive settings are unchanged."
                )
            )
        else:
            self.keepalive_notice.setText(
                tr("Uses the server-alive settings from OpenSSH and your SSH config.")
            )
        if self.selection_error is None:
            options = self.selected_options
            if options.is_default:
                self.summary.setText(
                    tr("Uses SSH config defaults. Changes apply to the next connection.")
                )
            else:
                timeout = (
                    tr("SSH config")
                    if options.connect_timeout is None
                    else tr("{p0} s", p0=f"{options.connect_timeout}")
                )
                keepalive = tr("SSH config")
                if options.server_alive_interval == 0:
                    keepalive = "disabled"
                elif options.server_alive_interval is not None:
                    keepalive = tr(
                        "{p0} s / {p1} probes",
                        p0=f"{options.server_alive_interval}",
                        p1=f"{options.server_alive_count_max}",
                    )
                self.summary.setText(
                    tr(
                        "Timeout: {p0} · Keepalive: {p1}. Applies to the next connection.",
                        p0=f"{timeout}",
                        p1=f"{keepalive}",
                    )
                )
        self.changed.emit()
