"""Explicit single-gateway SSH routes without managing gateway credentials."""

import re

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr, validation_text
from termana.models.ssh_jump import (
    MAX_SSH_JUMP_HOST_CHARACTERS,
    MAX_SSH_JUMP_USERNAME_CHARACTERS,
    SshJumpMode,
    SshJumpOptions,
)
from termana.ui_text import plain_text_tooltip
from termana.widgets.form_controls import FormComboBox, FormSpinBox
from termana.widgets.wrapping_label import WrappingLabel


class _RouteSummaryLabel(WrappingLabel):
    """Elide oversized endpoint tokens explicitly; retain the exact route for access."""

    def __init__(self) -> None:
        super().__init__()
        self._full_text = ""
        self.setObjectName("mutedText")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(plain_text_tooltip(text))
        self.setAccessibleDescription(text)
        self._refresh_text()

    def _refresh_text(self) -> None:
        text = self._full_text
        if self.isVisible():
            # QLabel only wraps at word boundaries. A long user@host:port token
            # must visibly lose its middle, never silently clip a port suffix.
            width = max(1, self.contentsRect().width() - 4)
            metrics = self.fontMetrics()
            text = re.sub(
                r"\S+",
                lambda match: metrics.elidedText(match[0], Qt.TextElideMode.ElideMiddle, width),
                text,
            )
        if text != self.text():
            self.setText(text)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._refresh_text()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._refresh_text()


class SshJumpSettings(QWidget):
    """Keep inherited, explicitly direct, and single-hop routes distinct."""

    changed = Signal()
    expanded_changed = Signal(bool)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        ssh_jump: SshJumpOptions | None = None,
    ) -> None:
        super().__init__(parent)
        options = SshJumpOptions() if ssh_jump is None else ssh_jump
        if type(options) is not SshJumpOptions:
            raise ValueError("Expected SSH jump options")
        self.setObjectName("sshJumpSettings")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toggle = QToolButton(self)
        self.toggle.setObjectName("sshJumpSettingsToggle")
        self.toggle.setText(tr("Jump host"))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(tr("Jump host"))
        self.toggle.setAccessibleDescription(tr("Expand SSH route and gateway settings"))
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.summary = _RouteSummaryLabel()
        self.summary.setAccessibleName(tr("SSH route summary"))
        layout.addWidget(self.summary)

        self.details = QWidget(self)
        form = QFormLayout(self.details)
        self._gateway_form = form
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.route_mode = FormComboBox()
        self.route_mode.setObjectName("sshJumpRouteMode")
        self.route_mode.setAccessibleName(tr("SSH route"))
        self.route_mode.setSizeAdjustPolicy(FormComboBox.SizeAdjustPolicy.AdjustToContents)
        for label, mode in (
            (tr("Use SSH config"), SshJumpMode.CONFIG),
            (tr("Direct connection"), SshJumpMode.DIRECT),
            (tr("Through a jump host"), SshJumpMode.JUMP),
        ):
            self.route_mode.addItem(label, mode.value)
        self._add_row(form, tr("Route"), self.route_mode)

        self.host_input = self._text_input(tr("Jump host"), "sshJumpHostInput")
        self.host_input.setPlaceholderText(tr("IP, DNS name, or OpenSSH alias"))
        self.host_input.setAccessibleDescription(
            tr("One gateway only. Use an ASCII host or SSH alias, or IPv6 without brackets.")
        )
        self.host_input.setMaxLength(MAX_SSH_JUMP_HOST_CHARACTERS + 1)
        self.host_input.setText(options.host or "")
        self.host_label = self._add_row(form, tr("Host"), self.host_input)

        self.username_input = self._text_input(tr("Gateway username"), "sshJumpUsernameInput")
        self.username_input.setPlaceholderText(tr("SSH config / local username"))
        self.username_input.setAccessibleDescription(
            tr(
                "Optional gateway account. Leaving it empty uses SSH config or the local username, not the destination username."
            )
        )
        self.username_input.setMaxLength(MAX_SSH_JUMP_USERNAME_CHARACTERS + 1)
        self.username_input.setText(options.username or "")
        self.username_label = self._add_row(form, tr("Gateway username"), self.username_input)

        self.port_input = FormSpinBox()
        self.port_input.setObjectName("sshJumpPortInput")
        self.port_input.setAccessibleName(tr("Gateway port"))
        self.port_input.setAccessibleDescription(
            tr("Optional gateway port. SSH config uses the gateway configuration or port 22.")
        )
        self.port_input.setRange(0, 65535)
        self.port_input.setSpecialValueText(tr("SSH config"))
        self.port_input.setValue(options.port or 0)
        self.port_label = self._add_row(form, tr("Gateway port"), self.port_input)

        self.gateway_notice = self._notice(
            tr(
                "One gateway only; enter IPv6 without brackets. Destination keys, cipher settings are not inherited by the gateway. Embedded requires an explicit gateway username; an empty port uses 22."
            )
        )
        form.addRow(self.gateway_notice)
        self.authentication_notice = self._notice(
            tr(
                "Embedded gateway authentication uses a separate inline password prompt or explicit saved-password choice. System mode uses Termana SSH config and the local agent; import gateway settings from Terminal → Import gateway SSH settings. User/system SSH configs are not inherited. Destination credentials are never reused for the gateway."
            )
        )
        for notice, short in (
            (self.gateway_notice, "Connect through one gateway. Its keys and sign-in are separate from the destination."),
            (self.authentication_notice, "System: import gateway settings first. Embedded: sign in to the gateway separately."),
        ):
            details = notice.text()
            notice.setToolTip(plain_text_tooltip(details))
            notice.setAccessibleDescription(details)
            notice.setText(tr(short))
        form.addRow(self.authentication_notice)
        self.route_notice = self._notice()
        form.addRow(self.route_notice)
        layout.addWidget(self.details)
        self.details.installEventFilter(self)

        self.route_mode.setCurrentIndex(self.route_mode.findData(options.mode.value))
        self.route_mode.currentIndexChanged.connect(self._update_state)
        self.host_input.textChanged.connect(self._update_state)
        self.username_input.textChanged.connect(self._update_state)
        self.port_input.valueChanged.connect(self._update_state)
        self.toggle.toggled.connect(self._set_expanded)
        self.toggle.setChecked(not options.is_default)
        self._set_expanded(self.toggle.isChecked())
        self._update_state()

    @staticmethod
    def _notice(text: str = "") -> QLabel:
        label = WrappingLabel(text)
        label.setObjectName("mutedText")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        # Host aliases and the route summary may contain long unbroken words.
        # Let text wrap within the available viewport instead of imposing its
        # preferred width on the entire form.
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    @staticmethod
    def _text_input(name: str, object_name: str) -> QLineEdit:
        control = QLineEdit()
        control.setObjectName(object_name)
        control.setAccessibleName(name)
        control.setMinimumWidth(0)
        control.setClearButtonEnabled(True)
        return control

    @staticmethod
    def _add_row(form: QFormLayout, text: str, control: QWidget) -> QLabel:
        label = QLabel(text)
        label.setBuddy(control)
        # Gateway labels may need two lines at large application font sizes.
        # Without wrapping, QFormLayout combines the longest label and the
        # port's minimum width, making a narrow scrolling dialog too wide.
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setMinimumWidth(0)
        form.addRow(label, control)
        return label

    @property
    def selected_options(self) -> SshJumpOptions:
        try:
            mode = SshJumpMode(self.route_mode.currentData())
        except (ValueError, TypeError):
            raise ValueError(tr("Choose a valid SSH route.")) from None
        if mode is not SshJumpMode.JUMP:
            return SshJumpOptions(mode=mode)
        return SshJumpOptions(
            mode=mode,
            host=self.host_input.text(),
            username=self.username_input.text() or None,
            port=self.port_input.value() or None,
        )

    @property
    def selection_error(self) -> str | None:
        try:
            _ = self.selected_options
        except ValueError as error:
            return validation_text(str(error))
        return None

    @property
    def invalid_control(self) -> QWidget:
        """Identify the first invalid field using the same model validation as saving."""
        if self.route_mode.currentData() not in tuple(mode.value for mode in SshJumpMode):
            return self.route_mode
        try:
            SshJumpOptions(mode=SshJumpMode.JUMP, host=self.host_input.text())
        except ValueError:
            return self.host_input
        try:
            SshJumpOptions(
                mode=SshJumpMode.JUMP,
                host=self.host_input.text(),
                port=self.port_input.value() or None,
            )
        except ValueError:
            return self.port_input
        return self.username_input

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details.setVisible(expanded)
        self.expanded_changed.emit(expanded)

    def eventFilter(self, watched, event: QEvent) -> bool:
        if watched is self.details:
            if event.type() == QEvent.Type.Resize:
                self._sync_gateway_layout()
            elif event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
                # Child controls receive their inherited font after the parent.
                # Recompute once those metrics and size hints have caught up.
                QTimer.singleShot(0, self, self._sync_gateway_layout)
        return super().eventFilter(watched, event)

    def _sync_gateway_layout(self) -> None:
        """Stack the entire gateway form when one shared label column crowds inputs."""
        label_width = max(
            label.fontMetrics().horizontalAdvance(label.text())
            for label in (self.host_label, self.username_label, self.port_label)
        )
        input_width = max(
            self.route_mode.minimumSizeHint().width(),
            self.port_input.minimumSizeHint().width(),
            self.host_input.fontMetrics().horizontalAdvance("gateway.example.com") + 32,
        )
        required_width = label_width + self._gateway_form.horizontalSpacing() + input_width
        policy = (
            QFormLayout.RowWrapPolicy.WrapAllRows
            if self.details.contentsRect().width() < required_width
            else QFormLayout.RowWrapPolicy.WrapLongRows
        )
        if self._gateway_form.rowWrapPolicy() != policy:
            self._gateway_form.setRowWrapPolicy(policy)

    def _update_state(self) -> None:
        is_jump = self.route_mode.currentData() == SshJumpMode.JUMP.value
        for control in (
            self.host_label,
            self.host_input,
            self.username_label,
            self.username_input,
            self.port_label,
            self.port_input,
            self.gateway_notice,
            self.authentication_notice,
        ):
            control.setVisible(is_jump)
            control.setEnabled(is_jump)
        error = self.selection_error
        if error is not None:
            summary = error
        else:
            options = self.selected_options
            if options.mode is SshJumpMode.CONFIG:
                summary = tr("Route: SSH config. System mode uses Termana config only.")
            elif options.mode is SshJumpMode.DIRECT:
                summary = tr("Route: direct to destination. Ignores SSH config proxies.")
            else:
                summary = tr(
                    "Route: this computer → {p0} → destination.", p0=f"{options.proxy_jump_value}"
                )
        self.summary.set_full_text(summary)
        if is_jump:
            notice = tr(
                "System gateway settings come from Termana SSH config after explicit import. Changes apply to new connections only."
            )
        elif self.route_mode.currentData() == SshJumpMode.DIRECT.value:
            notice = tr("Ignores ProxyJump and ProxyCommand from SSH config for the destination.")
        else:
            notice = tr("System uses Termana config only. Embedded needs an explicit route or a supported-config import.")
        self.route_notice.setText(notice)
        self._sync_gateway_layout()
        self.changed.emit()
