"""Local-only SSH identity choices without opening or copying private keys."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from termana.i18n import tr, validation_text
from termana.models.ssh_identity import (
    MAX_SSH_IDENTITY_FILE_CHARACTERS,
    SshAuthenticationMode,
    SshIdentityOptions,
)
from termana.ui_text import plain_text_tooltip
from termana.widgets.form_controls import FormComboBox
from termana.widgets.wrapping_label import WrappingLabel


class SshIdentitySettings(QWidget):
    """Preserve the selected engine's defaults until explicitly overridden."""

    changed = Signal()
    expanded_changed = Signal(bool)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        ssh_identity: SshIdentityOptions | None = None,
    ) -> None:
        super().__init__(parent)
        options = SshIdentityOptions() if ssh_identity is None else ssh_identity
        if type(options) is not SshIdentityOptions:
            raise ValueError("Expected SSH identity options")
        self.setObjectName("sshIdentitySettings")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toggle = QToolButton(self)
        self.toggle.setObjectName("sshIdentitySettingsToggle")
        self.toggle.setText(tr("SSH keys"))
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(tr("SSH keys"))
        self.toggle.setAccessibleDescription(tr("Expand local key file and SSH agent settings"))
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
        self.authentication_mode = self._choice(
            tr("SSH authentication method"), "sshAuthenticationMode"
        )
        for label, mode in (
            (tr("Default"), SshAuthenticationMode.CONFIG),
            (tr("Key only"), SshAuthenticationMode.KEY_ONLY),
            (tr("Password only"), SshAuthenticationMode.PASSWORD_ONLY),
        ):
            self.authentication_mode.addItem(label, mode.value)
        self.authentication_mode.setCurrentIndex(
            self.authentication_mode.findData(options.authentication.value)
        )
        self._add_row(form, tr("Sign-in method"), self.authentication_mode)
        self.method_hint = self._notice()
        form.addRow(self.method_hint)
        self.method_details = self._notice(
            tr(
                "Key only never falls back to a host password; a key passphrase can still be requested. Password only does not offer keys. Both explicit modes disable keyboard-interactive/MFA. Embedded key-only requires a selected key or explicitly enabled Unix agent."
            )
        )
        self.authentication_mode.setToolTip(plain_text_tooltip(self.method_details.text()))
        self.authentication_mode.setAccessibleDescription(self.method_details.text())

        self.identity_file_input = QLineEdit()
        self.identity_file_input.setObjectName("sshIdentityFileInput")
        self.identity_file_input.setAccessibleName(tr("SSH private key file path"))
        self.identity_file_input.setAccessibleDescription(
            tr(
                "Optional absolute path to a local private key. Clear to use SSH config. This editor does not read or copy the file. The selected SSH engine validates it when connecting."
            )
        )
        self.identity_file_input.setPlaceholderText(tr("Optional private key"))
        self.identity_file_input.setMinimumWidth(0)
        self.identity_file_input.setClearButtonEnabled(True)
        # Qt counts UTF-16 units, while the model counts Unicode code points.
        # Reserve two units per allowed character plus one excess character:
        # valid non-BMP paths stay intact and any truncation remains oversized.
        self.identity_file_input.setMaxLength(2 * (MAX_SSH_IDENTITY_FILE_CHARACTERS + 1))
        self.identity_file_input.setText(options.identity_file or "")
        self.browse_button = QPushButton(tr("Browse…"))
        self.browse_button.setObjectName("browseSshIdentityButton")
        self.browse_button.setProperty("actionRole", "secondary")
        self.browse_button.setAccessibleName(tr("Browse for an SSH private key file"))
        self.browse_button.setAutoDefault(False)
        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(8)
        key_row.addWidget(self.identity_file_input, 1)
        key_row.addWidget(self.browse_button)
        key_label = QLabel(tr("Key file"))
        key_label.setBuddy(self.identity_file_input)
        form.addRow(key_label, key_row)
        form.addRow(self._notice(tr("Private key on this computer; optional. Checked when you connect.")))
        self.key_notice = self._notice(
            tr(
                "System OpenSSH adds this file to the keys in your SSH config; it does not replace them. Embedded uses the selected file. The key is checked when you connect."
            )
        )
        self.identity_file_input.setToolTip(plain_text_tooltip(self.key_notice.text()))

        self.identities_mode = self._choice(tr("SSH key selection"), "sshIdentitiesMode")
        self.identities_mode.addItem(tr("Default"), "config")
        self.identities_mode.addItem(tr("Configured keys only"), "configured")
        self.identities_mode.addItem(tr("Allow other agent keys"), "all")
        self._add_row(form, tr("Key selection"), self.identities_mode)
        form.addRow(self._notice(tr("Choose whether SSH may also offer other keys from your agent.")))
        self.identities_notice = self._notice(
            tr(
                "Configured keys only limits unrelated agent keys. It does not mean only one key, and does not disable password authentication."
            )
        )
        self.identities_mode.setToolTip(plain_text_tooltip(self.identities_notice.text()))
        self.identities_mode.setAccessibleDescription(self.identities_notice.text())

        self.agent_mode = self._choice(tr("SSH agent use"), "sshIdentityAgentMode")
        self.agent_mode.addItem(tr("Default"), "config")
        self.agent_mode.addItem(tr("Disabled for this session"), "disabled")
        self._add_row(form, tr("SSH agent"), self.agent_mode)
        form.addRow(self._notice(tr("Use the local key agent, or disable it for this session. No agent forwarding.")))
        self.agent_notice = self._notice(
            tr(
                "Disabling the agent affects this session only. Neither choice enables agent forwarding."
            )
        )
        self.agent_mode.setToolTip(plain_text_tooltip(self.agent_notice.text()))
        self.agent_mode.setAccessibleDescription(self.agent_notice.text())
        self.storage_notice = self._notice(
            tr(
                "These settings stay on this computer and are not exported. The editor does not read or copy your private key, or change the agent's keys. Changes apply to new connections."
            )
        )
        self.authentication_notice = self._notice(
            tr(
                "With Default, choosing a saved login uses its password when allowed. For System OpenSSH key sign-in, choose Other user. Key only skips saved passwords and asks for the username in the terminal."
            )
        )
        self.authentication_notice.setAccessibleName(tr("SSH key authentication help"))
        layout.addWidget(self.details)

        self.help_toggle = QToolButton(self)
        self.help_toggle.setObjectName("sshIdentityHelpToggle")
        self.help_toggle.setText(tr("SSH key details"))
        self.help_toggle.setAccessibleName(tr("SSH key details"))
        self.help_toggle.setAutoRaise(True)
        self.help_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.help_toggle.setCheckable(True)
        self.help_toggle.setArrowType(Qt.ArrowType.RightArrow)
        form.addRow(self.help_toggle)
        self.help_details = QWidget(self.details)
        self.help_details.setObjectName("sshIdentityHelpDetails")
        help_layout = QVBoxLayout(self.help_details)
        help_layout.setContentsMargins(0, 6, 0, 0)
        help_layout.setSpacing(12)
        for notice in (
            self.method_details,
            self.key_notice,
            self.identities_notice,
            self.agent_notice,
            self.storage_notice,
            self.authentication_notice,
        ):
            help_layout.addWidget(notice)
        form.addRow(self.help_details)
        self.help_details.hide()
        self.help_toggle.toggled.connect(self._set_help_expanded)

        self.identities_mode.setCurrentIndex(
            0 if options.identities_only is None else 1 if options.identities_only else 2
        )
        self.agent_mode.setCurrentIndex(1 if options.agent_disabled else 0)
        self.identity_file_input.textChanged.connect(self._update_state)
        self.identities_mode.currentIndexChanged.connect(self._update_state)
        self.agent_mode.currentIndexChanged.connect(self._update_state)
        self.authentication_mode.currentIndexChanged.connect(self._update_state)
        self.browse_button.clicked.connect(self._browse)
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
        return label

    @staticmethod
    def _choice(name: str, object_name: str) -> FormComboBox:
        control = FormComboBox()
        control.setObjectName(object_name)
        control.setAccessibleName(name)
        # The popup can show full labels; the closed control must be able to
        # shrink inside narrow forms with large fonts/long translated choices.
        control.setSizeAdjustPolicy(
            FormComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        control.setMinimumContentsLength(3)
        control.setMinimumWidth(0)
        return control

    @staticmethod
    def _add_row(form: QFormLayout, text: str, control: QWidget) -> None:
        label = QLabel(text)
        label.setBuddy(control)
        form.addRow(label, control)

    @property
    def selected_options(self) -> SshIdentityOptions:
        selected = self.authentication_mode.currentData()
        if selected not in tuple(mode.value for mode in SshAuthenticationMode):
            raise ValueError(tr("Choose a valid SSH authentication method."))
        authentication = SshAuthenticationMode(selected)
        identities_mode = self.identities_mode.currentData()
        agent_mode = self.agent_mode.currentData()
        if identities_mode not in ("config", "configured", "all") or agent_mode not in (
            "config",
            "disabled",
        ):
            raise ValueError(tr("Choose valid SSH key and agent settings."))
        try:
            return SshIdentityOptions(
                identity_file=self.identity_file_input.text() or None,
                identities_only=(
                    None if identities_mode == "config" else identities_mode == "configured"
                ),
                agent_disabled=agent_mode == "disabled",
                authentication=authentication,
            )
        except ValueError:
            if authentication is not SshAuthenticationMode.PASSWORD_ONLY:
                raise
            # Keep a malformed key draft editable if the user switches back,
            # but never persist an invalid disabled field or block password login.
            return SshIdentityOptions(authentication=authentication)

    @property
    def selection_error(self) -> str | None:
        try:
            _ = self.selected_options
        except ValueError as error:
            return validation_text(str(error))
        return None

    @property
    def invalid_control(self) -> QWidget:
        """Locate a malformed choice without touching the filesystem."""
        if self.authentication_mode.currentData() not in tuple(
            mode.value for mode in SshAuthenticationMode
        ):
            return self.authentication_mode
        if self.identities_mode.currentData() not in ("config", "configured", "all"):
            return self.identities_mode
        if self.agent_mode.currentData() not in ("config", "disabled"):
            return self.agent_mode
        return self.identity_file_input

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            tr("Choose SSH private key"),
            "",
            tr("All files (*)"),
        )
        if path:
            self.identity_file_input.setText(path)

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details.setVisible(expanded)
        self.expanded_changed.emit(expanded)

    def _set_help_expanded(self, expanded: bool) -> None:
        self.help_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.help_details.setVisible(expanded)

    def _update_state(self) -> None:
        key_enabled = (
            self.authentication_mode.currentData() != SshAuthenticationMode.PASSWORD_ONLY.value
        )
        for control in (
            self.identity_file_input,
            self.browse_button,
            self.identities_mode,
            self.agent_mode,
        ):
            control.setEnabled(key_enabled)
        error = self.selection_error
        self.summary.setVisible(error is not None)
        if error is not None:
            self.summary.setText(error)
        elif self.selected_options.is_default:
            self.summary.setText(tr("Default authentication."))
        else:
            self.summary.setText(
                tr(
                    "{p0}. Custom local SSH settings; next connection.",
                    p0=f"{self.authentication_mode.currentText()}",
                )
            )
        self.method_hint.setText(
            {
                SshAuthenticationMode.CONFIG.value: tr("Uses the default sign-in method."),
                SshAuthenticationMode.KEY_ONLY.value: tr(
                    "Uses a key, not an account password. MFA is unavailable."
                ),
                SshAuthenticationMode.PASSWORD_ONLY.value: tr(
                    "Uses only a password. Keys and MFA are unavailable."
                ),
            }.get(self.authentication_mode.currentData(), "")
        )
        self.changed.emit()
