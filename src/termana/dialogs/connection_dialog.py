"""Dialog for creating a remote connection profile."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Final
from uuid import UUID

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from termana.dialog_geometry import fit_dialog_to_screen
from termana.i18n import count_noun, tr, validation_text
from termana.models.connection import (
    DEFAULT_SSH_PORT,
    MAX_CONNECTION_HOST_CHARACTERS,
    MAX_CONNECTION_NAME_CHARACTERS,
    MAX_NETWORK_PORT,
    MIN_NETWORK_PORT,
    ConnectionProfile,
    ConnectionProtocol,
    TelnetEncoding,
    WordDeleteMode,
    default_port_for_protocol,
)
from termana.models.rdp_options import RDP_ZOOMS, RdpOptions
from termana.models.settings import SshEngine
from termana.models.ssh_algorithms import SshAlgorithms
from termana.models.ssh_identity import SshIdentityOptions
from termana.models.ssh_jump import SshJumpMode, SshJumpOptions, validate_ssh_route_host
from termana.models.ssh_options import SshConnectionOptions
from termana.models.ssh_tunnels import SshTunnelOptions
from termana.rdp.endpoint import RDP_RESOLUTIONS, RdpEndpoint, validate_display_size
from termana.rdp.fields import RdpIdentityLineEdit
from termana.telnet_encoding_labels import telnet_encoding_dialog_label
from termana.widgets.connection_color_picker import ConnectionColorPicker
from termana.widgets.connection_protocol_selector import ConnectionProtocolSelector
from termana.widgets.connection_sections import ConnectionSections, ConnectionSettingsScrollArea
from termana.widgets.form_controls import FormComboBox, FormSpinBox
from termana.widgets.inline_error_area import InlineErrorArea
from termana.widgets.ssh_algorithm_settings import SshAlgorithmSettings
from termana.widgets.ssh_cipher_settings import SshCipherSettings
from termana.widgets.ssh_connection_settings import SshConnectionSettings
from termana.widgets.ssh_identity_settings import SshIdentitySettings
from termana.widgets.ssh_jump_settings import SshJumpSettings
from termana.widgets.ssh_tunnel_settings import SshTunnelSettings
from termana.widgets.themed_checkbox import ThemedCheckBox

MINIMUM_DIALOG_WIDTH: Final = 480
DEFAULT_DIALOG_WIDTH: Final = 640
DEFAULT_DIALOG_HEIGHT: Final = 680
ROOT_CONNECTION_FOLDER_LABEL: Final = "User sessions"
TELNET_SECURITY_NOTICE: Final = (
    "Telnet is unencrypted. Usernames, passwords, and all session data can be read "
    "in transit. Sign-in is completed interactively after connecting, and Termana "
    "does not store passwords."
)


class ConnectionDialog(QDialog):
    """Collect and validate settings for a new or existing connection."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        profile: ConnectionProfile | None = None,
        initial_profile: ConnectionProfile | None = None,
        save_handler: Callable[[ConnectionProfile, UUID | None], bool] | None = None,
        folder_choices: Sequence[tuple[UUID, str]] = (),
        initial_folder_id: UUID | None = None,
        new_ssh_route: SshJumpMode = SshJumpMode.CONFIG,
        account_choices: Sequence = (),
        manage_accounts: Callable | None = None,
        ssh_engine: SshEngine = SshEngine.SYSTEM,
        initial_protocol: str | None = None,
    ) -> None:
        """Build a blank creation form or prefill an existing profile."""
        if initial_profile is not None and (
            type(initial_profile) is not ConnectionProfile or profile is not None
        ):
            raise ValueError("A new-profile template cannot be combined with an edited profile")
        if save_handler is not None and not callable(save_handler):
            raise TypeError("save_handler must be callable or None")
        if type(new_ssh_route) is not SshJumpMode or new_ssh_route is SshJumpMode.JUMP:
            raise ValueError("A new connection route must be config or direct")
        include_rdp = True
        if initial_protocol is not None and (
            initial_protocol not in ("ssh", "telnet", "rdp")
            or profile is not None or initial_profile is not None
        ):
            raise ValueError("Initial protocol is supported only for a blank new connection")
        super().__init__(parent)
        seed_profile = profile if profile is not None else initial_profile
        self._save_handler = save_handler
        self._rdp_endpoint: RdpEndpoint | None = None

        self.setObjectName("connectionDialog")
        self.setWindowTitle(tr("Edit connection") if profile else tr("New connection"))
        self.setModal(True)
        self.setMinimumWidth(MINIMUM_DIALOG_WIDTH)
        self.setSizeGripEnabled(True)
        self.resize(DEFAULT_DIALOG_WIDTH, DEFAULT_DIALOG_HEIGHT)
        self._original_profile = profile
        self._connection_profile: ConnectionProfile | None = None
        self._selected_folder_id: UUID | None = None
        self._was_accepted = False
        self._validation_control: QWidget | None = None
        self._current_protocol = ConnectionProtocol.SSH
        self._port_uses_protocol_default = True
        self._updating_protocol_default = False
        self._initial_geometry_applied = False
        self._initial_geometry_timer = QTimer(self)
        self._initial_geometry_timer.setSingleShot(True)
        self._initial_geometry_timer.timeout.connect(self._fit_initial_geometry)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.body_scroll = ConnectionSettingsScrollArea()
        self.body_scroll.setObjectName("connectionDialogBodyScroll")
        self.body_scroll.setAccessibleName(tr("Connection settings"))
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setMinimumHeight(96)
        self.body_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.body_scroll.setAutoFillBackground(False)
        self.body_scroll.viewport().setObjectName("connectionDialogBodyViewport")
        self.body_scroll.viewport().setAutoFillBackground(False)

        self._telnet_reveal_timer = QTimer(self)
        self._telnet_reveal_timer.setSingleShot(True)
        self._telnet_reveal_timer.timeout.connect(self._reveal_telnet_encoding)
        self._validation_reveal_timer = QTimer(self)
        self._validation_reveal_timer.setSingleShot(True)
        self._validation_reveal_timer.timeout.connect(self._reveal_validation_error)

        self.body_widget = QWidget()
        self.body_widget.setObjectName("connectionDialogBody")
        self.body_widget.setAutoFillBackground(False)
        self.body_widget.setMinimumWidth(0)
        self.body_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        body_layout = QVBoxLayout(self.body_widget)
        body_layout.setContentsMargins(24, 22, 24, 18)
        body_layout.setSpacing(14)

        description_text = (
            tr("Update this connection profile. Authentication happens when the session opens.")
            if profile
            else tr("Create a connection profile. Authentication happens when the session opens.")
        )
        description = QLabel(description_text)
        if initial_profile is not None:
            description.setText(
                tr(
                    "Create a separate profile; this session stays unchanged. Destination login, passwords and tunnel approval are not copied. Local key paths and gateway login/settings are retained — review before saving."
                )
            )
        description.setObjectName("mutedText")
        description.setWordWrap(True)

        form = QFormLayout()
        form.setObjectName("connectionFormLayout")
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.name_input = QLineEdit()
        self.name_input.setObjectName("connectionNameInput")
        self.name_input.setPlaceholderText(tr("My server"))
        self.name_input.setMaxLength(MAX_CONNECTION_NAME_CHARACTERS)

        self.folder_input = FormComboBox()
        self.folder_input.setObjectName("connectionFolderInput")
        self.folder_input.setAccessibleName(tr("Connection folder"))
        self.folder_input.setAccessibleDescription(
            tr("Choose User sessions or a saved folder path for this connection.")
        )
        self.folder_input.addItem(tr(ROOT_CONNECTION_FOLDER_LABEL), "")
        seen_folder_ids: set[UUID] = set()
        for folder_id, path_label in folder_choices:
            if not isinstance(folder_id, UUID):
                raise ValueError("Connection folder choice ID must be a UUID")
            if folder_id in seen_folder_ids:
                raise ValueError("Connection folder choice IDs must be unique")
            if not isinstance(path_label, str) or not path_label.strip():
                raise ValueError("Connection folder path label cannot be empty")
            seen_folder_ids.add(folder_id)
            self.folder_input.addItem(path_label, str(folder_id))

        if initial_folder_id is not None and not isinstance(initial_folder_id, UUID):
            raise ValueError("Initial connection folder ID must be a UUID or None")
        initial_folder_data = "" if initial_folder_id is None else str(initial_folder_id)
        initial_folder_index = self.folder_input.findData(initial_folder_data)
        if initial_folder_index < 0:
            raise ValueError("Initial connection folder is not available")
        self.folder_input.setCurrentIndex(initial_folder_index)

        self.protocol_input = ConnectionProtocolSelector(include_rdp=include_rdp)
        self.protocol_input.setObjectName("connectionProtocolInput")
        self.protocol_input.setAccessibleName(tr("Connection protocol"))
        self.protocol_input.setAccessibleDescription(
            tr("Choose SSH, Telnet or an RDP remote desktop.") if include_rdp
            else tr("Choose encrypted SSH or unencrypted Telnet.")
        )

        self.host_input = QLineEdit()
        self.host_input.setObjectName("connectionHostInput")
        self.host_input.setPlaceholderText(tr("server.example.com or 192.168.1.10"))
        self.host_input.setMaxLength(MAX_CONNECTION_HOST_CHARACTERS)

        self.port_input = FormSpinBox()
        self.port_input.setObjectName("connectionPortInput")
        self.port_input.setRange(MIN_NETWORK_PORT, MAX_NETWORK_PORT)
        self.port_input.setValue(DEFAULT_SSH_PORT)

        self.telnet_encoding_input = FormComboBox()
        self.telnet_encoding_input.setObjectName("telnetEncodingInput")
        self.telnet_encoding_input.setAccessibleName(tr("Telnet text encoding"))
        self.telnet_encoding_input.setAccessibleDescription(
            tr("Choose how Termana converts text to and from bytes for this Telnet connection.")
        )
        self.telnet_encoding_input.setToolTip(
            tr("Use UTF-8 unless the remote system requires a legacy character encoding")
        )
        for encoding in TelnetEncoding:
            self.telnet_encoding_input.addItem(
                tr(telnet_encoding_dialog_label(encoding)),
                encoding.value,
            )

        self.telnet_security_notice = QLabel(tr(TELNET_SECURITY_NOTICE))
        self.telnet_security_notice.setObjectName("telnetSecurityNotice")
        self.telnet_security_notice.setAccessibleName(tr("Telnet security notice"))
        self.telnet_security_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.telnet_security_notice.setWordWrap(True)
        self.telnet_security_notice.hide()

        name_label = QLabel(tr("Name"))
        name_label.setBuddy(self.name_input)
        form.addRow(name_label, self.name_input)

        folder_label = QLabel(tr("Folder"))
        folder_label.setBuddy(self.folder_input)
        form.addRow(folder_label, self.folder_input)
        self.ssh_account_input = FormComboBox()
        self.ssh_account_input.setObjectName("sshAccountInput")
        self.ssh_account_input.setAccessibleName("SSH account")
        account_row = QWidget()
        account_layout = QHBoxLayout(account_row)
        account_layout.setContentsMargins(0, 0, 0, 0)
        account_layout.addWidget(self.ssh_account_input, 1)
        selected_account = seed_profile.ssh_account_id if profile is not None else None

        def populate_accounts(choices, selected):
            self.ssh_account_input.clear()
            self.ssh_account_input.addItem("Ask when connecting", None)
            for account in choices:
                self.ssh_account_input.addItem(f"{account.name} — {account.username}",
                                               str(account.account_id))
            value = str(selected) if selected is not None else None
            if value is not None and self.ssh_account_input.findData(value) < 0:
                self.ssh_account_input.addItem("Unavailable account — select another", value)
            self.ssh_account_input.setCurrentIndex(max(0, self.ssh_account_input.findData(value)))

        populate_accounts(account_choices, selected_account)
        if manage_accounts is not None:
            manage_button = QPushButton("Manage…")
            manage_button.setProperty("actionRole", "secondary")
            manage_button.setAutoDefault(False)
            def manage():
                selected = self.ssh_account_input.currentData()
                choices = manage_accounts()
                if choices is not None:
                    populate_accounts(choices, UUID(selected) if selected else None)
            manage_button.clicked.connect(manage)
            account_layout.addWidget(manage_button)
        self.ssh_account_row = account_row
        self.ssh_account_label = QLabel("SSH account")
        self.protocol_label = QLabel(tr("Protocol"))
        self.protocol_label.setBuddy(self.protocol_input)

        self.endpoint_widget = QWidget()
        self.endpoint_widget.setObjectName("connectionEndpointHeader")
        endpoint_layout = QVBoxLayout(self.endpoint_widget)
        endpoint_layout.setContentsMargins(24, 14, 24, 12)
        endpoint_layout.setSpacing(10)
        endpoint_layout.addWidget(self.protocol_label)
        endpoint_layout.addWidget(self.protocol_input, 0, Qt.AlignmentFlag.AlignLeft)
        endpoint_form = QGridLayout()
        endpoint_form.setContentsMargins(0, 0, 0, 0)
        endpoint_form.setHorizontalSpacing(14)
        endpoint_form.setVerticalSpacing(5)
        endpoint_form.setColumnStretch(0, 1)

        self.host_label = QLabel(tr("Host"))
        self.host_label.setBuddy(self.host_input)
        endpoint_form.addWidget(self.host_label, 0, 0)
        endpoint_form.addWidget(self.host_input, 1, 0)
        self.route_host_error = QLabel()
        self.route_host_error.setObjectName("connectionValidationError")
        self.route_host_error.setAccessibleName(tr("Destination route host validation error"))
        self.route_host_error.setTextFormat(Qt.TextFormat.PlainText)
        self.route_host_error.setWordWrap(True)
        self.route_host_error.setMinimumWidth(0)
        self.route_host_error.hide()
        endpoint_form.addWidget(self.route_host_error, 2, 0, 1, 2)

        self.port_label = QLabel(tr("Port"))
        self.port_label.setBuddy(self.port_input)
        self.port_input.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        endpoint_form.addWidget(self.port_label, 0, 1)
        endpoint_form.addWidget(self.port_input, 1, 1)
        endpoint_layout.addLayout(endpoint_form)

        self.telnet_encoding_label = QLabel(tr("Text &encoding"))
        self.telnet_encoding_label.setBuddy(self.telnet_encoding_input)
        form.addRow(self.telnet_encoding_label, self.telnet_encoding_input)

        self.word_delete_input = FormComboBox()
        self.word_delete_input.setObjectName("wordDeleteModeInput")
        self.word_delete_input.setAccessibleName(tr("Option+Backspace (macOS)"))
        self.word_delete_input.addItem(tr("Default (Esc + DEL)"), WordDeleteMode.ESCAPE_DEL.value)
        self.word_delete_input.addItem(tr("Ctrl+W"), WordDeleteMode.CTRL_W.value)
        self.word_delete_label = QLabel(tr("Option+Backspace (macOS)"))
        self.word_delete_label.setBuddy(self.word_delete_input)
        self.word_delete_row = QWidget()
        word_delete_layout = QVBoxLayout(self.word_delete_row)
        word_delete_layout.setContentsMargins(0, 0, 0, 0)
        word_delete_layout.addWidget(self.word_delete_input)
        self.word_delete_help = QLabel(tr("Try Ctrl+W if the default does not delete words."))
        self.word_delete_help.setWordWrap(True)
        self.word_delete_help.setMinimumWidth(0)
        word_delete_layout.addWidget(self.word_delete_help)
        self.word_delete_input.setAccessibleDescription(self.word_delete_help.text())
        form.addRow(self.word_delete_label, self.word_delete_row)
        self.color_picker = ConnectionColorPicker()
        color_label = QLabel(tr("Connection color"))
        color_label.setBuddy(self.color_picker.input)
        form.addRow(color_label, self.color_picker)

        self.ssh_cipher_settings = SshCipherSettings(
            ssh_cipher=seed_profile.ssh_cipher if seed_profile is not None else None,
            engine=ssh_engine,
        )
        self.ssh_cipher_label = QLabel(tr("Encryption"))
        self.ssh_cipher_label.setBuddy(self.ssh_cipher_settings.cipher_input)
        cipher_page = QWidget()
        cipher_layout = QVBoxLayout(cipher_page)
        cipher_layout.setContentsMargins(0, 0, 0, 0)
        cipher_layout.addWidget(self.ssh_cipher_label)
        cipher_layout.addWidget(self.ssh_cipher_settings)
        self.ssh_algorithm_settings = SshAlgorithmSettings(
            seed_profile.ssh_algorithms if seed_profile else SshAlgorithms(), engine=ssh_engine
        )
        cipher_layout.addWidget(self.ssh_algorithm_settings)
        # Discovery emits changed too; only edited may dismiss a user's error.
        self.ssh_algorithm_settings.changed.connect(self._sync_save_button)
        self.ssh_algorithm_settings.edited.connect(self._clear_validation_error)

        self.ssh_connection_settings = SshConnectionSettings(
            ssh_options=seed_profile.ssh_options if seed_profile is not None else None
        )

        self.ssh_identity_settings = SshIdentitySettings(
            ssh_identity=seed_profile.ssh_identity if seed_profile is not None else None
        )
        self.ssh_identity_settings.layout().addWidget(self.ssh_account_label)
        self.ssh_identity_settings.layout().addWidget(account_row)

        self.ssh_jump_settings = SshJumpSettings(
            ssh_jump=seed_profile.ssh_jump
            if seed_profile is not None
            else SshJumpOptions(new_ssh_route)
        )

        self.ssh_tunnel_settings = SshTunnelSettings(
            ssh_tunnels=seed_profile.ssh_tunnels if seed_profile is not None else None
        )

        self.sections = ConnectionSections(self.body_widget)
        for key, group, title_text, explanation, control in (
            (
                "route",
                "network",
                tr("Jump host"),
                tr("Direct connection, SSH config or a gateway."),
                self.ssh_jump_settings,
            ),
            (
                "identity",
                "ssh",
                tr("SSH keys"),
                "",
                self.ssh_identity_settings,
            ),
            (
                "behavior",
                "network",
                tr("Connection behavior"),
                tr("Timeouts and encrypted keepalive probes."),
                self.ssh_connection_settings,
            ),
            (
                "tunnels",
                "tunnels",
                tr("Local tunnels"),
                tr("Forward local ports through this SSH connection."),
                self.ssh_tunnel_settings,
            ),
            (
                "encryption",
                "ssh",
                tr("Encryption"),
                tr("Automatic negotiation or a specific cipher."),
                cipher_page,
            ),
        ):
            self.sections.add_section(
                key,
                title_text,
                explanation,
                control,
                group=group,
                show_summary=key != "identity",
            )
        for settings in (
            self.ssh_connection_settings,
            self.ssh_identity_settings,
            self.ssh_jump_settings,
            self.ssh_tunnel_settings,
        ):
            # Standalone widgets retain their collapsible API. In this dialog
            # the selected tab replaces the redundant nested disclosure.
            settings.toggle.hide()
        self.sections.section_changed.connect(self._section_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setObjectName("connectionDialogButtons")

        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setObjectName("saveConnectionButton")
        self.save_button.setAccessibleName(tr("Save connection"))
        self.save_button.setEnabled(False)

        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("cancelConnectionButton")
        self.cancel_button.setAccessibleName(tr("Cancel connection changes"))

        self.error_area = InlineErrorArea()
        self.error_area.setObjectName("connectionErrorArea")
        self.validation_error = self.error_area.label
        self.validation_error.setObjectName("connectionValidationError")
        self.validation_error.setAccessibleName(tr("Connection validation error"))
        self.validation_error.setTextFormat(Qt.TextFormat.PlainText)
        self.validation_error.setWordWrap(True)
        self.validation_error.hide()

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.name_input.textChanged.connect(self._update_save_button)
        self.host_input.textChanged.connect(self._update_save_button)
        self.port_input.valueChanged.connect(self._mark_port_as_custom)
        self.folder_input.currentIndexChanged.connect(self._clear_validation_error)
        self.protocol_input.currentIndexChanged.connect(self._handle_protocol_changed)
        self.telnet_encoding_input.currentIndexChanged.connect(self._clear_validation_error)
        self.ssh_cipher_settings.changed.connect(self._handle_cipher_changed)
        self.ssh_cipher_settings.edited.connect(self._clear_validation_error)
        self.ssh_connection_settings.changed.connect(self._update_save_button)
        self.ssh_identity_settings.changed.connect(self._update_save_button)
        self.ssh_jump_settings.changed.connect(self._update_save_button)
        self.ssh_tunnel_settings.changed.connect(self._update_save_button)
        self.ssh_tunnel_settings.focus_requested.connect(self._handle_tunnel_focus_requested)

        self.profile_widget = QWidget()
        profile_layout = QVBoxLayout(self.profile_widget)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_layout.setSpacing(14)
        profile_layout.addWidget(description)
        profile_layout.addLayout(form)
        body_layout.addWidget(self.profile_widget)
        self.rdp_notice = QLabel(tr(
            "Passwords and certificate decisions are not stored in this profile."
        ))
        self.rdp_notice.setObjectName("rdpConnectionNotice")
        self.rdp_notice.setWordWrap(True)
        self.rdp_notice.setTextFormat(Qt.TextFormat.PlainText)
        body_layout.addWidget(self.rdp_notice)
        self.rdp_display_row = QWidget()
        display_form = QFormLayout(self.rdp_display_row)
        display_form.setContentsMargins(0, 0, 0, 0)
        display_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        display_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        display_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.rdp_display_input = FormComboBox()
        self.rdp_display_input.setObjectName("rdpDesktopSizeInput")
        self.rdp_display_input.setAccessibleName(tr("Desktop size"))
        for size in RDP_RESOLUTIONS:
            self.rdp_display_input.addItem(f"{size[0]} x {size[1]}", size)
        self.rdp_display_input.setCurrentIndex(RDP_RESOLUTIONS.index((1280, 720)))
        display_form.addRow(tr("Desktop size"), self.rdp_display_input)
        self.rdp_zoom_input = FormComboBox()
        self.rdp_zoom_input.setObjectName("rdpProfileZoomInput")
        self.rdp_zoom_input.setAccessibleName(tr("Default view"))
        for zoom in RDP_ZOOMS:
            self.rdp_zoom_input.addItem(tr("Fit to window") if zoom is None else f"{zoom}%", zoom)
        display_form.addRow(tr("Default view"), self.rdp_zoom_input)
        self.rdp_view_only_input = ThemedCheckBox(tr("Start in view-only mode"))
        self.rdp_fullscreen_input = ThemedCheckBox(tr("Start in full screen"))
        display_form.addRow(self.rdp_view_only_input)
        display_form.addRow(self.rdp_fullscreen_input)
        def rdp_hint(caption):
            hint = QLabel(tr(caption))
            hint.setWordWrap(True)
            hint.setObjectName("mutedText")
            display_form.addRow(hint)

        self.rdp_dynamic_input = ThemedCheckBox(tr("Resize remote desktop"))
        self.rdp_dynamic_input.setObjectName("rdpDynamicResolutionInput")
        display_form.addRow(self.rdp_dynamic_input)
        rdp_hint("Fits the panel, up to 1920 x 1080. Requires server support.")
        self.rdp_keyboard_input = FormComboBox()
        for title, value in (("Send to remote", "remote"), ("Keep on this computer", "local"),
                             ("Full screen only", "fullscreen")):
            self.rdp_keyboard_input.addItem(tr(title), value)
        self.rdp_keyboard_input.setAccessibleName(tr("Keyboard shortcuts"))
        self.rdp_keyboard_input.setObjectName("rdpKeyboardPolicyInput")
        self.rdp_keyboard_input.setToolTip(tr("App tab shortcuts stay local. Use Input for Windows security keys."))
        display_form.addRow(tr("Keyboard shortcuts"), self.rdp_keyboard_input)
        rdp_hint(tr("Release keyboard: {p0}. System shortcuts may stay local.",
                    p0=QKeySequence("Ctrl+Shift+F12").toString(QKeySequence.SequenceFormat.NativeText)))
        self.rdp_admin_input = ThemedCheckBox(tr("Admin session"))
        self.rdp_admin_input.setObjectName("rdpAdminSessionInput")
        display_form.addRow(self.rdp_admin_input)
        rdp_hint("Windows Server only. Requires permission; does not grant admin rights.")
        self.rdp_clipboard_send_input = ThemedCheckBox(tr("Send text"))
        self.rdp_clipboard_receive_input = ThemedCheckBox(tr("Receive text"))
        self.rdp_clipboard_send_input.setAccessibleName(tr("Send text to Windows"))
        self.rdp_clipboard_receive_input.setAccessibleName(tr("Receive text from Windows"))
        self.rdp_clipboard_send_input.setObjectName("rdpClipboardToRemoteInput")
        self.rdp_clipboard_receive_input.setObjectName("rdpClipboardToLocalInput")
        display_form.addRow(self.rdp_clipboard_send_input)
        display_form.addRow(self.rdp_clipboard_receive_input)
        rdp_hint("Send to Windows or receive from it. Plain text only, up to 16 KB.")
        self.rdp_dynamic_input.toggled.connect(self._rdp_dynamic_changed)
        self.rdp_username_input = RdpIdentityLineEdit()
        self.rdp_domain_input = RdpIdentityLineEdit()
        for label, control in ((tr("Username (optional)"), self.rdp_username_input),
                               (tr("Domain (optional)"), self.rdp_domain_input)):
            control.setMaxLength(512)
            control.setAccessibleName(label)
            display_form.addRow(label, control)
            control.textChanged.connect(self._update_save_button)
        self.rdp_display_input.currentIndexChanged.connect(self._update_save_button)
        self.rdp_zoom_input.currentIndexChanged.connect(self._update_save_button)
        body_layout.addWidget(self.rdp_display_row)
        self.rdp_limits = QLabel(tr(
            "NLA is required. Files, drives and other devices are not shared."
        ))
        self.rdp_limits.setObjectName("mutedText")
        self.rdp_limits.setWordWrap(True)
        self.rdp_limits.setTextFormat(Qt.TextFormat.PlainText)
        body_layout.addWidget(self.rdp_limits)
        body_layout.addWidget(self.telnet_security_notice)
        body_layout.addStretch()

        scroll_content = QWidget()
        scroll_content.setObjectName("connectionDialogPages")
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(0)
        scroll_layout.addWidget(self.sections)
        self.body_scroll.setWidget(scroll_content)
        root_layout.addWidget(self.endpoint_widget)
        self.settings_tabs_row = QWidget()
        tabs_layout = QHBoxLayout(self.settings_tabs_row)
        tabs_layout.setContentsMargins(24, 0, 24, 0)
        tabs_layout.setSpacing(0)
        self.sections.tabs.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        tabs_layout.addWidget(self.sections.tabs)
        tabs_layout.addStretch(1)
        root_layout.addWidget(self.settings_tabs_row)
        root_layout.addWidget(self.body_scroll, 1)

        self.footer_widget = QWidget()
        self.footer_widget.setObjectName("connectionDialogFooter")
        footer_layout = QVBoxLayout(self.footer_widget)
        footer_layout.setContentsMargins(24, 14, 24, 22)
        footer_layout.setSpacing(10)
        footer_layout.addWidget(self.error_area)
        footer_layout.addWidget(buttons)
        root_layout.addWidget(self.footer_widget)
        self.error_area.set_height_limit_provider(
            lambda: self.height()
            - self.endpoint_widget.sizeHint().height()
            - self.settings_tabs_row.sizeHint().height()
            - self.body_scroll.minimumHeight()
            - buttons.sizeHint().height()
            - footer_layout.contentsMargins().top()
            - footer_layout.contentsMargins().bottom()
            - footer_layout.spacing()
        )
        for left, right in zip(
            self.protocol_input.buttons, self.protocol_input.buttons[1:], strict=False
        ):
            QWidget.setTabOrder(left, right)
        QWidget.setTabOrder(self.protocol_input.buttons[-1], self.host_input)
        QWidget.setTabOrder(self.host_input, self.port_input)
        QWidget.setTabOrder(self.port_input, self.sections.tabs)

        if seed_profile is not None:
            options = seed_profile.rdp_options
            # Qt compares opaque tuple userData by identity, not Python tuple
            # equality. A newly decoded profile must restore the preset by value.
            self.rdp_display_input.setCurrentIndex(RDP_RESOLUTIONS.index((options.width, options.height)))
            self.rdp_zoom_input.setCurrentIndex(self.rdp_zoom_input.findData(options.zoom))
            self.rdp_view_only_input.setChecked(options.view_only)
            self.rdp_fullscreen_input.setChecked(options.full_screen)
            self.rdp_dynamic_input.setChecked(options.dynamic_resolution)
            self.rdp_keyboard_input.setCurrentIndex(self.rdp_keyboard_input.findData(options.keyboard_mode))
            self.rdp_admin_input.setChecked(options.admin_session)
            self.rdp_clipboard_send_input.setChecked(options.clipboard_to_remote)
            self.rdp_clipboard_receive_input.setChecked(options.clipboard_to_local)
            self.rdp_username_input.setText(options.username)
            self.rdp_domain_input.setText(options.domain)
            self._populate_from_profile(seed_profile)
        else:
            self._update_protocol_notice()
            self._sync_save_button()
        if initial_protocol is not None:
            self.protocol_input.setCurrentIndex(self.protocol_input.findData(initial_protocol))

    @property
    def rdp_endpoint(self) -> RdpEndpoint | None:
        """Return endpoint metadata after acceptance; saving does not connect."""
        return self._rdp_endpoint

    @property
    def connection_profile(self) -> ConnectionProfile:
        """Return the validated profile after the dialog was accepted."""
        if self._connection_profile is None:
            raise RuntimeError("Connection dialog has not been accepted")
        return self._connection_profile

    @property
    def selected_folder_id(self) -> UUID | None:
        """Return the selected catalog destination only after acceptance."""
        if not self._was_accepted:
            raise RuntimeError("Connection dialog has not been accepted")
        return self._selected_folder_id

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # QDialogButtonBox's native polish/layout establishes platform order.
        # Own the logical footer order afterwards, independently of that order.
        QWidget.setTabOrder(self.save_button, self.cancel_button)
        if not self._initial_geometry_applied:
            handle = self.windowHandle()
            if handle is not None:
                handle.installEventFilter(self)
            self._schedule_initial_geometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "error_area"):
            self.error_area._schedule_fit()

    def eventFilter(self, watched, event: QEvent) -> bool:
        if (
            not self._initial_geometry_applied
            and watched is self.windowHandle()
            and event.type() == QEvent.Type.Expose
            and watched.isExposed()
        ):
            self._schedule_initial_geometry()
        return super().eventFilter(watched, event)

    def _schedule_initial_geometry(self) -> None:
        if not self._initial_geometry_timer.isActive():
            self._initial_geometry_timer.start(0)

    def _fit_initial_geometry(self) -> None:
        handle = self.windowHandle()
        if not self.isVisible() or handle is None or not handle.isExposed():
            return
        # X11 decorations may not exist at Show; first exposure supplies their
        # frame extents. Do not poll or keep refitting after user interaction.
        self._initial_geometry_applied = True
        handle.removeEventFilter(self)
        fit_dialog_to_screen(self)

    def _update_save_button(self) -> None:
        """Enable saving only when all required text fields are filled."""
        self._clear_validation_error()
        self._sync_save_button()

    def _sync_save_button(self) -> None:
        """Background capability results must not erase a host/name validation error."""
        has_name = bool(self.name_input.text().strip())
        has_host = bool(self.host_input.text().strip())
        route_host_error = self._target_route_host_error()
        self.route_host_error.setText(route_host_error or "")
        self.route_host_error.setVisible(route_host_error is not None)
        self.save_button.setEnabled(has_name and has_host and self._ssh_settings_error() is None
                                    and self._rdp_settings_error() is None)
        self._refresh_section_summaries()

    def _refresh_section_summaries(self) -> None:
        """State at a glance; never show passwords, key material or discard a draft."""
        defaults, configured = tr("Defaults"), tr("Configured")
        widgets = {
            "route": self.ssh_jump_settings,
            "identity": self.ssh_identity_settings,
            "behavior": self.ssh_connection_settings,
            "tunnels": self.ssh_tunnel_settings,
            "encryption": self.ssh_cipher_settings,
        }
        for key, widget in widgets.items():
            error = widget.selection_error
            if key == "encryption":
                error = error or self.ssh_algorithm_settings.selection_error
                summary = (widget.selected_cipher or tr("Automatic")) if error is None else ""
                if error is None and not self.ssh_algorithm_settings.selected.is_default:
                    summary += " · Custom SSH algorithms"
            elif error is None:
                options = widget.selected_options
                if key == "route":
                    summary = {
                        SshJumpMode.CONFIG: tr("SSH config"),
                        SshJumpMode.DIRECT: tr("Direct connection"),
                        SshJumpMode.JUMP: tr("Via gateway: {host}", host=options.host or ""),
                    }[options.mode]
                elif key == "tunnels":
                    count = len(options.local_forwards)
                    summary = (
                        f"{count} {count_noun('rule', 'rules', count)}"
                        if count
                        else tr("No tunnels")
                    )
                else:
                    summary = defaults if options.is_default else configured
            else:
                summary = ""
            self.sections.set_summary(key, summary, error=error)

    def _section_changed(self, key: str) -> None:
        """Keep the tab row pinned while the chosen settings scroll below it."""
        for group, control in (
            ("network", self.ssh_jump_settings),
            ("network", self.ssh_connection_settings),
            ("ssh", self.ssh_identity_settings),
            ("tunnels", self.ssh_tunnel_settings),
        ):
            if group == key:
                control.toggle.setChecked(True)
        self.body_scroll.verticalScrollBar().setValue(0)
        QTimer.singleShot(0, self, self._reveal_current_section_focus)

    def _reveal_current_section_focus(self) -> None:
        target = QApplication.focusWidget()
        if target is not None and self.sections.isAncestorOf(target) and target.isVisible():
            self.body_scroll.ensureWidgetVisible(target, 0, 12)

    def _ssh_settings_error(self) -> str | None:
        if self._current_protocol is not ConnectionProtocol.SSH:
            return None
        return (
            self.ssh_cipher_settings.selection_error
            or self.ssh_algorithm_settings.selection_error
            or self.ssh_connection_settings.selection_error
            or self.ssh_identity_settings.selection_error
            or self.ssh_jump_settings.selection_error
            or self.ssh_tunnel_settings.selection_error
            or self._target_route_host_error()
        )

    def _target_route_host_error(self) -> str | None:
        if (
            self._current_protocol is not ConnectionProtocol.SSH
            or self.ssh_jump_settings.route_mode.currentData() != SshJumpMode.JUMP.value
            or not self.host_input.text().strip()
        ):
            return None
        try:
            validate_ssh_route_host(self.host_input.text().strip())
        except ValueError as error:
            return tr("Destination host: {p0}", p0=f"{error}")
        return None

    def _ssh_error_control(self) -> QWidget | None:
        """Locate an inline route/key error without overriding earlier cipher errors."""
        if self._current_protocol is not ConnectionProtocol.SSH:
            return None
        if self.ssh_algorithm_settings.selection_error:
            return self.ssh_algorithm_settings
        if self.ssh_cipher_settings.selection_error is not None:
            return (
                self.ssh_cipher_settings.legacy_consent
                if not self.ssh_cipher_settings.legacy_consent.isHidden()
                else self.ssh_cipher_settings.cipher_input
            )
        if self.ssh_connection_settings.selection_error is not None:
            return self.ssh_connection_settings.timeout_mode
        if self.ssh_identity_settings.selection_error is not None:
            return self.ssh_identity_settings.invalid_control
        if self.ssh_jump_settings.selection_error is not None:
            return self.ssh_jump_settings.invalid_control
        if self.ssh_tunnel_settings.selection_error is not None:
            return self.ssh_tunnel_settings.invalid_control
        if self._target_route_host_error() is not None:
            return self.host_input
        return None

    def _populate_from_profile(self, profile: ConnectionProfile) -> None:
        """Fill the form with a profile selected for editing."""
        self.name_input.setText(profile.name)
        self.host_input.setText(profile.host)

        protocol_index = self.protocol_input.findData(profile.protocol.value)
        if protocol_index >= 0:
            previous_signal_state = self.protocol_input.blockSignals(True)
            try:
                self.protocol_input.setCurrentIndex(protocol_index)
            finally:
                self.protocol_input.blockSignals(previous_signal_state)
        self._current_protocol = profile.protocol
        self._updating_protocol_default = True
        try:
            self.port_input.setValue(profile.port)
        finally:
            self._updating_protocol_default = False
        self._port_uses_protocol_default = profile.port == default_port_for_protocol(
            profile.protocol
        )
        encoding_index = self.telnet_encoding_input.findData(profile.telnet_encoding.value)
        if encoding_index >= 0:
            self.telnet_encoding_input.setCurrentIndex(encoding_index)
        self.word_delete_input.setCurrentIndex(self.word_delete_input.findData(profile.word_delete_mode.value))
        self.color_picker.set_color(profile.accent_color)
        self._update_protocol_notice()

        self._update_save_button()

    def _handle_protocol_changed(self, _index: int) -> None:
        """Apply a new conventional port only while the old default is untouched."""
        try:
            selected_protocol = ConnectionProtocol(str(self.protocol_input.currentData()))
        except ValueError:
            self._clear_validation_error()
            return

        if self._port_uses_protocol_default:
            self._updating_protocol_default = True
            try:
                self.port_input.setValue(
                    default_port_for_protocol(selected_protocol)
                )
            finally:
                self._updating_protocol_default = False
        self._current_protocol = selected_protocol
        self._update_protocol_notice()
        self._update_save_button()

    def _mark_port_as_custom(self, _value: int) -> None:
        """Remember explicit user intent even when it happens to equal a default."""
        if not self._updating_protocol_default:
            self._port_uses_protocol_default = False

    def _update_protocol_notice(self) -> None:
        """Show only the selected protocol's controls without losing other drafts."""
        is_telnet = self._current_protocol is ConnectionProtocol.TELNET
        is_ssh = self._current_protocol is ConnectionProtocol.SSH
        is_rdp = self.protocol_input.currentData() == "rdp"
        self.word_delete_label.setVisible(not is_rdp)
        self.word_delete_row.setVisible(not is_rdp)
        self.profile_widget.setVisible(True)
        self.rdp_notice.setVisible(is_rdp)
        self.rdp_display_row.setVisible(is_rdp)
        self.rdp_limits.setVisible(is_rdp)
        self.settings_tabs_row.setVisible(not is_rdp)
        self.save_button.setText(tr("Save"))
        self.save_button.setAccessibleName(tr("Save connection"))
        self.name_input.setPlaceholderText(tr("My Windows server") if is_rdp else tr("My server"))
        self.telnet_encoding_label.setEnabled(is_telnet)
        self.telnet_encoding_input.setEnabled(is_telnet)
        self.telnet_encoding_label.setVisible(is_telnet)
        self.telnet_encoding_input.setVisible(is_telnet)
        self.telnet_security_notice.setVisible(is_telnet)
        self.ssh_cipher_label.setVisible(is_ssh)
        self.ssh_algorithm_settings.setVisible(is_ssh)
        self.ssh_account_row.setVisible(is_ssh)
        self.ssh_account_label.setVisible(is_ssh)
        self.ssh_cipher_settings.setVisible(is_ssh)
        self.ssh_cipher_settings.setEnabled(is_ssh)
        self.ssh_connection_settings.setVisible(is_ssh)
        self.ssh_connection_settings.setEnabled(is_ssh)
        self.ssh_identity_settings.setVisible(is_ssh)
        self.ssh_identity_settings.setEnabled(is_ssh)
        self.ssh_jump_settings.setVisible(is_ssh)
        self.ssh_jump_settings.setEnabled(is_ssh)
        self.ssh_tunnel_settings.setVisible(is_ssh)
        self.ssh_tunnel_settings.setEnabled(is_ssh)
        self.sections.set_ssh_available(is_ssh)
        if is_telnet and self.isVisible():
            self._telnet_reveal_timer.start(0)

    def _reveal_telnet_encoding(self) -> None:
        """Reveal the dynamic Telnet row after Qt has relaid out the scroll body."""
        if (
            self._current_protocol is ConnectionProtocol.TELNET
            and self.telnet_encoding_input.isVisible()
        ):
            self.body_scroll.ensureWidgetVisible(self.telnet_encoding_input, 0, 12)

    def _handle_cipher_changed(self) -> None:
        self._sync_save_button()
        if self.validation_error.isVisible():
            self._validation_reveal_timer.start(0)
            return
        if not self._cipher_has_focus():
            return
        if self._current_protocol is ConnectionProtocol.SSH and self.isVisible():
            QTimer.singleShot(0, self, self._reveal_cipher_selection)

    def _handle_tunnel_focus_requested(self, control: object) -> None:
        def reveal() -> None:
            # A removed row's queued reveal must not touch a deleted Qt wrapper.
            settings = self.ssh_tunnel_settings
            candidates = [settings.toggle, settings.add_button]
            candidates.extend(row.local_port for row in settings.rows)
            if any(control is candidate for candidate in candidates) and settings.isVisible():
                self.body_scroll.ensureWidgetVisible(control, 0, 12)

        QTimer.singleShot(0, self, reveal)

    def _reveal_cipher_selection(self) -> None:
        if not self.ssh_cipher_settings.isVisible() or not self._cipher_has_focus():
            return
        target = (
            self.ssh_cipher_settings.legacy_consent
            if self.ssh_cipher_settings.legacy_consent.isVisible()
            else self.ssh_cipher_settings.cipher_input
        )
        self.body_scroll.ensureWidgetVisible(target, 0, 12)

    def _cipher_has_focus(self) -> bool:
        focused = QApplication.focusWidget()
        return focused is not None and (
            focused is self.ssh_cipher_settings or self.ssh_cipher_settings.isAncestorOf(focused)
        )

    def _reveal_validation_error(self) -> None:
        """The footer keeps the message visible; scroll only its editable field."""
        if self.validation_error.isVisible():
            control = self._validation_control
            if control is not None and control.isVisible() and self.sections.isAncestorOf(control):
                self.body_scroll.ensureWidgetVisible(control, 0, 12)

    def _show_validation_error(self, message: str, control: QWidget | None = None) -> None:
        self._validation_control = control
        self.error_area.show_message(message)
        if control is not None:
            self.sections.reveal_control(control)
        self._validation_reveal_timer.start(0)

    def accept(self) -> None:
        """Validate the form, build its profile, and close successfully."""
        if self._was_accepted:
            return  # Queued clicks/Enter must not persist the same draft twice.
        self._update_save_button()
        if not self.save_button.isEnabled():
            if self._rdp_settings_error() is not None:
                self._show_validation_error(self._rdp_settings_error(), self.rdp_username_input)
                return
            settings_error = self._ssh_settings_error()
            if settings_error is not None:
                self._show_validation_error(settings_error, self._ssh_error_control())
                return
            field = self.name_input if not self.name_input.text().strip() else self.host_input
            self.sections.reveal_control(field)
            if self.sections.isAncestorOf(field):
                self.body_scroll.ensureWidgetVisible(field, 0, 12)
            return

        try:
            protocol = ConnectionProtocol(str(self.protocol_input.currentData()))
            telnet_encoding = (
                TelnetEncoding(str(self.telnet_encoding_input.currentData()))
                if protocol is ConnectionProtocol.TELNET
                else TelnetEncoding.UTF8
            )
            values = {
                "name": self.name_input.text().strip(),
                "host": self.host_input.text().strip(),
                "protocol": protocol,
                "rdp_options": (self._selected_rdp_options() if protocol is ConnectionProtocol.RDP
                                else RdpOptions()),
                "port": self.port_input.value(),
                "username": None,
                "ssh_account_id": (UUID(self.ssh_account_input.currentData())
                                   if protocol is ConnectionProtocol.SSH and
                                   self.ssh_account_input.currentData() else None),
                "ssh_algorithms": (self.ssh_algorithm_settings.selected
                                   if protocol is ConnectionProtocol.SSH else SshAlgorithms()),
                "telnet_encoding": telnet_encoding,
                "accent_color": self.color_picker.selected_color,
                "word_delete_mode": (WordDeleteMode(str(self.word_delete_input.currentData()))
                                     if protocol is not ConnectionProtocol.RDP else WordDeleteMode.ESCAPE_DEL),
                "ssh_cipher": (
                    self.ssh_cipher_settings.selected_cipher
                    if protocol is ConnectionProtocol.SSH
                    else None
                ),
                "ssh_options": (
                    self.ssh_connection_settings.selected_options
                    if protocol is ConnectionProtocol.SSH
                    else SshConnectionOptions()
                ),
                "ssh_identity": (
                    self.ssh_identity_settings.selected_options
                    if protocol is ConnectionProtocol.SSH
                    else SshIdentityOptions()
                ),
                "ssh_jump": (
                    self.ssh_jump_settings.selected_options
                    if protocol is ConnectionProtocol.SSH
                    else SshJumpOptions()
                ),
                "ssh_tunnels": (
                    self.ssh_tunnel_settings.selected_options
                    if protocol is ConnectionProtocol.SSH
                    else SshTunnelOptions()
                ),
            }
            folder_data = str(self.folder_input.currentData())
            selected_folder_id = UUID(folder_data) if folder_data else None
            if self._original_profile is None:
                profile = ConnectionProfile(**values)
            else:
                profile = replace(self._original_profile, **values)
        except ValueError as error:
            # Model diagnostics are owned, literal identifiers, not translated
            # user text. Keep validation in the model rather than duplicating it.
            message = str(error)
            control = (
                self.name_input
                if message.startswith("Connection name")
                else self.host_input
                if message.startswith("Connection host")
                else self.protocol_input
                if self.protocol_input.currentData() not in ("ssh", "telnet", "rdp")
                else None
            )
            self._show_validation_error(validation_text(message), control)
            return

        if (
            self._save_handler is not None
            and self._save_handler(profile, selected_folder_id) is not True
        ):
            self._show_validation_error(
                tr(
                    "The profile could not be saved. Check the selected folder and connection-file availability, then retry. Your changes are still here."
                )
            )
            return

        self._connection_profile = profile
        if profile.protocol is ConnectionProtocol.RDP:
            self._rdp_endpoint = RdpEndpoint(profile.host, profile.port,
                                            profile.rdp_options.width, profile.rdp_options.height)
        self._selected_folder_id = selected_folder_id
        self._was_accepted = True
        super().accept()

    def _rdp_settings_error(self):
        if self.protocol_input.currentData() != "rdp":
            return None
        try:
            self._selected_rdp_options()
        except ValueError as error:
            return tr(str(error))
        return None

    def _rdp_dynamic_changed(self, enabled):
        if enabled:
            self.rdp_zoom_input.setCurrentIndex(self.rdp_zoom_input.findData(None))
        self.rdp_zoom_input.setEnabled(not enabled)

    def _selected_rdp_options(self):
        width, height = validate_display_size(self.rdp_display_input.currentData())
        return RdpOptions(width, height, self.rdp_zoom_input.currentData(),
                          self.rdp_view_only_input.isChecked(), self.rdp_fullscreen_input.isChecked(),
                          self.rdp_username_input.text().strip(), self.rdp_domain_input.text().strip(),
                          clipboard_to_remote=self.rdp_clipboard_send_input.isChecked(),
                          clipboard_to_local=self.rdp_clipboard_receive_input.isChecked(),
                          dynamic_resolution=self.rdp_dynamic_input.isChecked(),
                          keyboard_mode=self.rdp_keyboard_input.currentData(),
                          admin_session=self.rdp_admin_input.isChecked())

    def _clear_validation_error(self) -> None:
        """Hide an obsolete validation message as soon as the form changes."""
        self.validation_error.clear()
        self.validation_error.hide()
        self.error_area.hide()
        self._validation_control = None
