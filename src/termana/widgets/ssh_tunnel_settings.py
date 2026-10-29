"""Local-forward rule editor, separate from runtime permission and SSH launch."""

from itertools import pairwise

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr
from termana.models.ssh_tunnels import MAX_SSH_LOCAL_FORWARDS, SshLocalForward, SshTunnelOptions
from termana.widgets.form_controls import FormSpinBox
from termana.widgets.wrapping_label import WrappingLabel


def _notice(text: str = "") -> QLabel:
    label = WrappingLabel(text)
    label.setObjectName("mutedText")
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


class _TunnelRuleEditor(QFrame):
    changed = Signal()
    remove_requested = Signal()

    def __init__(self, forward: SshLocalForward | None = None) -> None:
        super().__init__()
        self.setObjectName("sshTunnelRule")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        # A scrollable parent must grow its content, not compress the card below
        # its fields' native/style minimums while recalculating wrapped text.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.title, 1)
        self.remove_button = QPushButton(tr("Remove"))
        self.remove_button.setProperty("actionRole", "secondary")
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit())
        header.addWidget(self.remove_button)
        layout.addLayout(header)
        # These fields are always stacked. Using a two-column form with forced
        # wrapping can under-allocate the nested rows during relayout.
        form = QVBoxLayout()
        form.setSpacing(6)
        self.local_port = FormSpinBox()
        self.destination_host = QLineEdit()
        self.destination_host.setMinimumWidth(0)
        self.destination_host.setMaxLength(254)
        self.destination_host.setClearButtonEnabled(True)
        self.destination_host.setPlaceholderText(tr("127.0.0.1 or service.internal"))
        self.destination_port = FormSpinBox()
        for control in (self.local_port, self.destination_port):
            control.setRange(0, 65535)
            control.setSpecialValueText(tr("Choose port"))
        for title, control in (
            (tr("Port on this computer"), self.local_port),
            (tr("Destination host"), self.destination_host),
            (tr("Destination port"), self.destination_port),
        ):
            label = _notice(title)
            label.setBuddy(control)
            form.addWidget(label)
            form.addWidget(control)
        layout.addLayout(form)
        self.error = _notice()
        self.error.setObjectName("connectionValidationError")
        self.error.hide()
        layout.addWidget(self.error)
        self.local_port.setValue(forward.local_port if forward else 0)
        self.destination_host.setText(forward.destination_host if forward else "127.0.0.1")
        self.destination_port.setValue(forward.destination_port if forward else 0)
        self.destination_host.textChanged.connect(self._emit_changed)
        # Observe draft edits too, not just committed spinbox values.
        for control in (self.local_port, self.destination_port):
            control.valueChanged.connect(self._emit_changed)
            control.lineEdit().textChanged.connect(self._emit_changed)

    def _emit_changed(self) -> None:
        self.changed.emit()

    def set_index(self, index: int) -> None:
        self.title.setText(tr("Tunnel {p0}", p0=f"{index}"))
        self.remove_button.setAccessibleName(tr("Remove tunnel {p0}", p0=f"{index}"))
        for name, control in (
            (tr("local port"), self.local_port),
            (tr("destination host"), self.destination_host),
            (tr("destination port"), self.destination_port),
        ):
            control.setAccessibleName(tr("Tunnel {p0} {p1}", p0=f"{index}", p1=f"{name}"))

    def validation(self) -> tuple[str, QWidget] | None:
        if not self.local_port.hasAcceptableInput() or self.local_port.value() == 0:
            return tr("Choose a local port from 1 to 65535."), self.local_port
        try:
            SshLocalForward(self.local_port.value(), self.destination_host.text(), 1)
        except ValueError:
            return (
                tr("Enter an ASCII host name or IPv6 address without brackets."),
                self.destination_host,
            )
        if not self.destination_port.hasAcceptableInput() or self.destination_port.value() == 0:
            return tr("Choose a destination port from 1 to 65535."), self.destination_port
        return None

    def selected_forward(self) -> SshLocalForward:
        if error := self.validation():
            raise ValueError(error[0])
        return SshLocalForward(
            self.local_port.value(), self.destination_host.text(), self.destination_port.value()
        )


class SshTunnelSettings(QWidget):
    """Edit bounded local rules without opening sockets or approving a launch."""

    changed = Signal()
    expanded_changed = Signal(bool)
    focus_requested = Signal(object)

    def __init__(
        self, parent: QWidget | None = None, *, ssh_tunnels: SshTunnelOptions | None = None
    ):
        super().__init__(parent)
        options = SshTunnelOptions() if ssh_tunnels is None else ssh_tunnels
        if type(options) is not SshTunnelOptions:
            raise ValueError("Expected SSH tunnel options")
        self.setObjectName("sshTunnelSettings")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.toggle = QToolButton()
        self.toggle.setObjectName("sshTunnelSettingsToggle")
        self.toggle.setText(tr("Local tunnels"))
        self.toggle.setAccessibleName(tr("Local SSH tunnel rules"))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setCheckable(True)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.summary = _notice()
        layout.addWidget(self.summary)
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(10)
        self.launch_notice = _notice(
            tr(
                "Rules start with SSH after you confirm them when connecting. Saving rules does not open ports or grant permission to connect."
            )
        )
        details_layout.addWidget(self.launch_notice)
        details_layout.addWidget(
            _notice(
                tr(
                    "This computer (127.0.0.1) → SSH server → destination. The SSH server resolves the destination; 127.0.0.1 there means that server. Rules stay on this computer and are excluded from portable exports."
                )
            )
        )
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(10)
        details_layout.addLayout(self.rows_layout)
        self.add_button = QPushButton(tr("Add local tunnel"))
        self.add_button.setObjectName("addLocalTunnelButton")
        self.add_button.setProperty("actionRole", "secondary")
        self.add_button.setAccessibleName(tr("Add local SSH tunnel"))
        self.add_button.setAutoDefault(False)
        details_layout.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.details)
        self.rows: list[_TunnelRuleEditor] = []
        for forward in options.local_forwards:
            self._append_row(forward)
        self.add_button.clicked.connect(self.add_rule)
        self.toggle.toggled.connect(self._set_expanded)
        self.toggle.setChecked(not options.is_default)
        self._set_expanded(self.toggle.isChecked())
        self._update_state()

    def _append_row(self, forward: SshLocalForward | None = None) -> _TunnelRuleEditor:
        row = _TunnelRuleEditor(forward)
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        row.changed.connect(self._update_state)
        row.remove_requested.connect(lambda row=row: self.remove_rule(row))
        self._update_tab_order()
        return row

    def add_rule(self) -> None:
        if len(self.rows) >= MAX_SSH_LOCAL_FORWARDS:
            return
        row = self._append_row()
        self.toggle.setChecked(True)
        self._update_state()
        row.local_port.setFocus()
        self.focus_requested.emit(row.local_port)

    def remove_rule(self, row: _TunnelRuleEditor) -> None:
        if row not in self.rows:
            return
        index = self.rows.index(row)
        self.rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._update_tab_order()
        self._update_state()
        target = (
            self.rows[min(index, len(self.rows) - 1)].local_port if self.rows else self.add_button
        )
        target.setFocus()
        self.focus_requested.emit(target)

    def _errors(self) -> list[tuple[str, QWidget] | None]:
        ports: set[int] = set()
        errors = []
        for row in self.rows:
            error = row.validation()
            port = row.local_port.value()
            if error is None and port in ports:
                error = tr("Use a different local port for each tunnel."), row.local_port
            ports.add(port)
            errors.append(error)
        return errors

    @property
    def selected_options(self) -> SshTunnelOptions:
        if error := self.selection_error:
            raise ValueError(error)
        return SshTunnelOptions(tuple(row.selected_forward() for row in self.rows))

    @property
    def selection_error(self) -> str | None:
        for index, error in enumerate(self._errors(), 1):
            if error:
                return tr("Tunnel {p0}: {p1}", p0=f"{index}", p1=f"{error[0]}")
        return None

    @property
    def invalid_control(self) -> QWidget:
        return next((error[1] for error in self._errors() if error), self.add_button)

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details.setVisible(expanded)
        self.expanded_changed.emit(expanded)

    def _update_state(self) -> None:
        for index, (row, error) in enumerate(zip(self.rows, self._errors(), strict=True), 1):
            row.set_index(index)
            row.error.setText(error[0] if error else "")
            row.error.setVisible(error is not None)
        count = len(self.rows)
        self.add_button.setEnabled(count < MAX_SSH_LOCAL_FORWARDS)
        self.add_button.setToolTip(
            tr("Up to {p0} local tunnels per session", p0=f"{MAX_SSH_LOCAL_FORWARDS}")
        )
        self.summary.setText(
            self.selection_error
            or (
                tr(
                    "{p0} {p1} · confirmed when connecting",
                    p0=f"{count}",
                    p1=tr("local rule", context="tunnel-count")
                    if count == 1
                    else tr("local rules", context="tunnel-count"),
                )
                if count
                else tr("No local rules. SSH config is unchanged.")
            )
        )
        self.changed.emit()

    def _update_tab_order(self) -> None:
        """Keep dynamically added cards before Add and the enclosing dialog footer."""
        controls = [self.toggle]
        for row in self.rows:
            controls.extend(
                (row.remove_button, row.local_port, row.destination_host, row.destination_port)
            )
        controls.append(self.add_button)
        for before, after in pairwise(controls):
            QWidget.setTabOrder(before, after)
