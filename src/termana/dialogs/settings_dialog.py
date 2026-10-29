"""Dialog for editing application-wide preferences."""

from dataclasses import replace

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from termana.dialogs.terminal_highlighting_dialog import TerminalHighlightingDialog
from termana.i18n import tr
from termana.initial_window_fit import InitialWindowFit
from termana.models.settings import (
    MAX_TERMINAL_FONT_POINT_SIZE,
    MAX_TERMINAL_SCROLLBACK_LINES,
    MIN_TERMINAL_FONT_POINT_SIZE,
    MIN_TERMINAL_SCROLLBACK_LINES,
    ApplicationSettings,
    SshEngine,
    ThemeId,
)
from termana.qt_lifecycle import schedule_delete_later
from termana.terminal.font import available_terminal_font_families
from termana.theme import THEME_LABELS
from termana.ui_toolbar import WORKBENCH_TOOLS
from termana.ui_typography import scale_widget_font
from termana.widgets.form_controls import FormComboBox, FormSpinBox
from termana.widgets.inline_error_area import InlineErrorArea
from termana.widgets.local_terminal_settings import LocalTerminalPreferences
from termana.widgets.themed_checkbox import ThemedCheckBox
from termana.widgets.wrapping_label import WrappingLabel

SYSTEM_TERMINAL_FONT_LABEL = "System monospace"
UNAVAILABLE_FONT_SUFFIX = " (unavailable)"


class SettingsDialog(QDialog):
    """Collect a settings snapshot and delegate committing to its owner."""

    save_requested = Signal(object)
    theme_preview_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: ApplicationSettings,
    ) -> None:
        """Build a modal editor initialized from the applied snapshot."""
        super().__init__(parent)
        self._initial_settings = settings
        self.setObjectName("settingsDialog")
        self.setWindowTitle(tr("Settings"))
        self.setModal(True)
        self.setMinimumWidth(500)
        self.resize(640, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._appearance_scroll_area = self._scroll_area("settingsScrollArea")

        settings_body = QWidget()
        settings_body.setObjectName("settingsScrollContent")
        body_layout = QVBoxLayout(settings_body)
        body_layout.setContentsMargins(26, 24, 26, 18)
        body_layout.setSpacing(14)

        description = WrappingLabel(
            tr("Personalize Termana and choose defaults for future terminal sessions.")
        )
        description.setObjectName("mutedText")
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        appearance_title = QLabel(tr("Appearance"))
        appearance_title.setObjectName("settingsSectionTitle")
        scale_widget_font(appearance_title, 1.4, weight=QFont.Weight.Bold)
        body_layout.addWidget(appearance_title)
        body_layout.addWidget(description)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.theme_input = FormComboBox()
        self.theme_input.setObjectName("themeInput")
        self.theme_input.setAccessibleName(tr("Color theme"))
        self.theme_input.setAccessibleDescription(
            tr(
                "Preview application and terminal colors immediately. Save keeps the choice; Cancel restores the previous theme."
            )
        )
        self.theme_input.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.theme_input.setMinimumContentsLength(12)
        for theme_id, label in THEME_LABELS.items():
            self.theme_input.addItem(tr(label), theme_id.value)
        selected_index = self.theme_input.findData(settings.theme.value)
        if selected_index < 0:
            raise ValueError("Settings contain an unsupported theme")
        self.theme_input.setCurrentIndex(selected_index)

        theme_label = QLabel(tr("Theme"))
        theme_label.setBuddy(self.theme_input)
        form.addRow(theme_label, self.theme_input)
        self._add_help(form, "Changes colors only; your sessions stay open.")

        self.terminal_font_family_input = FormComboBox()
        self.terminal_font_family_input.setObjectName("terminalFontFamilyInput")
        self.terminal_font_family_input.setAccessibleName(tr("Terminal font family"))
        self.terminal_font_family_input.setAccessibleDescription(
            tr("Choose a monospace font, or use the system monospace font.")
        )
        self.terminal_font_family_input.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.terminal_font_family_input.setMinimumContentsLength(12)
        self.terminal_font_family_input.addItem(tr(SYSTEM_TERMINAL_FONT_LABEL), None)
        for family in available_terminal_font_families():
            self.terminal_font_family_input.addItem(family, family)

        saved_family = settings.terminal_font.family
        family_index = self.terminal_font_family_input.findData(saved_family)
        if family_index < 0 and saved_family is not None:
            family_index = self.terminal_font_family_input.count()
            self.terminal_font_family_input.addItem(
                f"{saved_family}{tr(UNAVAILABLE_FONT_SUFFIX)}",
                saved_family,
            )
            unavailable_description = tr(
                "The saved font {p0} is unavailable as a terminal font. Keep it selected to preserve the preference or choose another font.",
                p0=f"{saved_family}",
            )
            self.terminal_font_family_input.setItemData(
                family_index,
                unavailable_description,
                Qt.ItemDataRole.AccessibleDescriptionRole,
            )
            self.terminal_font_family_input.setAccessibleDescription(unavailable_description)
        self.terminal_font_family_input.setCurrentIndex(family_index)

        terminal_font_family_label = QLabel(tr("Terminal font"))
        terminal_font_family_label.setBuddy(self.terminal_font_family_input)
        form.addRow(terminal_font_family_label, self.terminal_font_family_input)
        self._add_help(form, "Text style used by all terminal sessions.")

        self.terminal_font_point_size_input = FormSpinBox()
        self.terminal_font_point_size_input.setObjectName("terminalFontPointSizeInput")
        self.terminal_font_point_size_input.setAccessibleName(tr("Terminal font size"))
        self.terminal_font_point_size_input.setAccessibleDescription(
            tr("Choose the terminal font size in points.")
        )
        self.terminal_font_point_size_input.setRange(
            MIN_TERMINAL_FONT_POINT_SIZE,
            MAX_TERMINAL_FONT_POINT_SIZE,
        )
        self.terminal_font_point_size_input.setSuffix(tr(" pt"))
        self.terminal_font_point_size_input.setValue(settings.terminal_font.point_size)

        terminal_font_point_size_label = QLabel(tr("Font size"))
        terminal_font_point_size_label.setBuddy(self.terminal_font_point_size_input)
        form.addRow(terminal_font_point_size_label, self.terminal_font_point_size_input)
        self._add_help(form, "Larger values make terminal text easier to read.")
        body_layout.addLayout(form)
        toolbar_title = QLabel(tr("Toolbar buttons"))
        toolbar_title.setObjectName("settingsSectionTitle")
        body_layout.addWidget(toolbar_title)
        toolbar_hint = WrappingLabel(tr(
            "Choose visible buttons. All commands remain available in menus."
        ))
        toolbar_hint.setObjectName("mutedText")
        body_layout.addWidget(toolbar_hint)
        self.toolbar_inputs = {}
        for tool in WORKBENCH_TOOLS:
            checkbox = ThemedCheckBox(tr(tool.label, context="toolbar"))
            checkbox.setObjectName(f"toolbarChoice_{tool.action_attribute}")
            checkbox.setChecked(tool.action_attribute in settings.toolbar_actions)
            self.toolbar_inputs[tool.action_attribute] = checkbox
            body_layout.addWidget(checkbox)

        self._terminal_scroll_area = self._scroll_area("terminalSettingsScrollArea")
        terminal_body = QWidget()
        terminal_body_layout = QVBoxLayout(terminal_body)
        terminal_body_layout.setContentsMargins(26, 24, 26, 18)
        terminal_body_layout.setSpacing(14)
        terminal_title = QLabel(tr("Terminal"))
        terminal_title.setObjectName("settingsSectionTitle")
        scale_widget_font(terminal_title, 1.4, weight=QFont.Weight.Bold)
        terminal_body_layout.addWidget(terminal_title)
        self.local_terminal_input = LocalTerminalPreferences(settings.local_terminal)
        terminal_body_layout.addWidget(self.local_terminal_input)

        terminal_form = QFormLayout()
        terminal_form.setContentsMargins(0, 0, 0, 0)
        terminal_form.setHorizontalSpacing(18)
        terminal_form.setVerticalSpacing(10)
        terminal_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        terminal_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        terminal_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.terminal_scrollback_lines_input = FormSpinBox()
        self.terminal_scrollback_lines_input.setObjectName("terminalScrollbackLinesInput")
        self.terminal_scrollback_lines_input.setAccessibleName(tr("Terminal scrollback lines"))
        self.terminal_scrollback_lines_input.setAccessibleDescription(
            tr(
                "Choose how many scrollback lines new terminal sessions retain. Set to zero to disable scrollback."
            )
        )
        self.terminal_scrollback_lines_input.setRange(
            MIN_TERMINAL_SCROLLBACK_LINES,
            MAX_TERMINAL_SCROLLBACK_LINES,
        )
        self.terminal_scrollback_lines_input.setSpecialValueText(tr("Disabled"))
        self.terminal_scrollback_lines_input.setSuffix(tr(" lines"))
        self.terminal_scrollback_lines_input.setValue(settings.terminal_scrollback_lines)

        terminal_scrollback_lines_label = QLabel(tr("Scrollback"))
        terminal_scrollback_lines_label.setBuddy(self.terminal_scrollback_lines_input)
        terminal_form.addRow(
            terminal_scrollback_lines_label,
            self.terminal_scrollback_lines_input,
        )
        self._add_help(terminal_form, "Past lines kept by new terminal sessions. Zero turns history off.")

        self.visual_bell_enabled_input = ThemedCheckBox(tr("Visual bell"))
        self.visual_bell_enabled_input.setObjectName("visualBellEnabledInput")
        self.visual_bell_enabled_input.setAccessibleName(tr("Visual bell"))
        visual_bell_description = tr(
            "Briefly flash the terminal and mark its background tab when a remote "
            "program sends a bell."
        )
        self.visual_bell_enabled_input.setAccessibleDescription(visual_bell_description)
        self.visual_bell_enabled_input.setToolTip(visual_bell_description)
        self.visual_bell_enabled_input.setChecked(settings.visual_bell_enabled)
        terminal_form.addRow(tr("Alerts"), self.visual_bell_enabled_input)
        self._add_help(terminal_form, "Flashes the terminal when a program sends an alert.")
        self.confirm_session_close_input = ThemedCheckBox(tr("Confirm session close"))
        self.confirm_session_close_input.setObjectName("confirmSessionCloseInput")
        self.confirm_session_close_input.setChecked(settings.confirm_session_close)
        terminal_form.addRow(tr("Sessions"), self.confirm_session_close_input)
        self._add_help(terminal_form, "Ask before closing a running terminal.")
        self.backspace_input = FormComboBox()
        self.backspace_input.setObjectName("backspaceModeInput")
        self.backspace_input.setAccessibleName(tr("Backspace sends"))
        self.backspace_input.addItem(tr("DEL (0x7f, default)"), False)
        self.backspace_input.addItem(tr("Control-H (^H, 0x08)"), True)
        self.backspace_input.setCurrentIndex(1 if settings.backspace_sends_ctrl_h else 0)
        terminal_form.addRow(tr("Backspace sends"), self.backspace_input)
        self._add_help(terminal_form, "Keep DEL unless your server needs Control-H for erasing text.")
        word_navigation = WrappingLabel(
            tr(
                "Move by words: Option + Left/Right on macOS; Ctrl + Left/Right on Windows and Linux."
            )
        )
        word_navigation.setObjectName("mutedText")
        terminal_form.addRow(word_navigation)
        self._terminal_highlighting = settings.terminal_highlighting
        self.highlighting_button = QPushButton("Highlighting…")
        self.highlighting_button.setProperty("actionRole", "secondary")
        self.highlighting_button.setAutoDefault(False)
        self.highlighting_button.clicked.connect(self._open_highlighting)
        terminal_form.addRow(self.highlighting_button)
        terminal_body_layout.addLayout(terminal_form)
        terminal_body_layout.addStretch()
        self._terminal_scroll_area.setWidget(terminal_body)

        self._ssh_scroll_area = self._scroll_area("sshSettingsScrollArea")
        ssh_body = QWidget()
        ssh_body_layout = QVBoxLayout(ssh_body)
        ssh_body_layout.setContentsMargins(26, 24, 26, 18)
        ssh_body_layout.setSpacing(14)
        ssh_title = QLabel(tr("SSH and files"))
        ssh_title.setObjectName("settingsSectionTitle")
        scale_widget_font(ssh_title, 1.4, weight=QFont.Weight.Bold)
        ssh_body_layout.addWidget(ssh_title)
        ssh_form = QFormLayout()
        ssh_form.setContentsMargins(0, 0, 0, 0)
        ssh_form.setHorizontalSpacing(18)
        ssh_form.setVerticalSpacing(10)
        ssh_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        ssh_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        ssh_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.ssh_engine_input = FormComboBox()
        self.ssh_engine_input.setObjectName("sshEngineInput")
        self.ssh_engine_input.setAccessibleName(tr("SSH engine for next launch"))
        self.ssh_engine_input.addItem(tr("Embedded SSH · SFTP"), SshEngine.EMBEDDED.value)
        self.ssh_engine_input.addItem(tr("System OpenSSH"), SshEngine.SYSTEM.value)
        self.ssh_engine_input.setCurrentIndex(
            self.ssh_engine_input.findData(settings.ssh_engine.value)
        )
        engine_hint = tr(
            "Applies after restarting Termana. Open sessions are unchanged. Embedded SSH "
            "is the default, supports Files, Direct routing or one explicit password-only gateway, "
            "and interactive host-key review. System OpenSSH is explicit compatibility mode "
            "with isolated Termana SSH config and explicitly imported gateway identities, "
            "with a separate Files connection and another sign-in if needed. "
            "Explicit --ssh-engine overrides this choice for one run."
        )
        self.ssh_engine_input.setToolTip(engine_hint)
        self.ssh_engine_input.setAccessibleDescription(engine_hint)
        engine_label = QLabel(tr("SSH engine · restart required"))
        engine_label.setWordWrap(True)
        engine_label.setBuddy(self.ssh_engine_input)
        ssh_form.addRow(engine_label, self.ssh_engine_input)
        engine_help = WrappingLabel(
            tr("Restart required. Embedded includes Files; System uses OpenSSH. Open sessions stay unchanged.")
        )
        engine_help.setObjectName("mutedText")
        engine_help.setWordWrap(True)
        ssh_form.addRow(engine_help)
        self.sftp_auto_open_input = ThemedCheckBox(tr("Auto-open Files"))
        self.sftp_auto_open_input.setObjectName("sftpAutoOpenInput")
        self.sftp_auto_open_input.setAccessibleName(tr("Automatically open remote files"))
        self.sftp_auto_open_input.setToolTip(
            tr(
                "Only for connected embedded SSH with SFTP. Hidden sidebars stay hidden; choosing Connections keeps that terminal in Connections."
            )
        )
        self.sftp_auto_open_input.setAccessibleDescription(self.sftp_auto_open_input.toolTip())
        self.sftp_auto_open_input.setChecked(settings.sftp_auto_open)
        ssh_form.addRow(tr("Remote files"), self.sftp_auto_open_input)
        sftp_hint = WrappingLabel(
            tr("Show server files after SSH connects. Hidden sidebars stay hidden.")
        )
        sftp_hint.setObjectName("mutedText")
        sftp_hint.setWordWrap(True)
        ssh_form.addRow(sftp_hint)
        ssh_body_layout.addLayout(ssh_form)
        ssh_body_layout.addStretch()
        self._ssh_scroll_area.setWidget(ssh_body)

        hint = WrappingLabel(
            tr(
                "Save applies your changes. Cancel discards them."
            )
        )
        hint.setObjectName("mutedText")
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setWordWrap(True)
        body_layout.addWidget(hint)

        self.error_area = InlineErrorArea()
        self.error_area.setObjectName("settingsErrorArea")
        self.error_label = self.error_area.label
        self.error_label.setObjectName("settingsError")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        body_layout.addStretch()
        self._appearance_scroll_area.setWidget(settings_body)
        self.settings_tabs = QTabWidget()
        # Each category already scrolls. Hidden-page minimum hints must not
        # squeeze the pinned error/actions footer in a short, large-font window.
        self.settings_tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.settings_tabs.setObjectName("settingsCategoryTabs")
        self.settings_tabs.setAccessibleName(tr("Settings categories"))
        self.settings_tabs.setDocumentMode(True)
        self.settings_tabs.tabBar().setDrawBase(False)
        self.settings_tabs.setUsesScrollButtons(True)
        self.settings_tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.settings_tabs.addTab(self._appearance_scroll_area, tr("Appearance"))
        self.settings_tabs.addTab(self._terminal_scroll_area, tr("Terminal"))
        self.settings_tabs.addTab(self._ssh_scroll_area, tr("SSH and files"))
        layout.addWidget(self.settings_tabs, 1)

        button_box = QDialogButtonBox()
        button_box.setObjectName("settingsDialogButtons")
        save_button = QPushButton(tr("Save"))
        save_button.setObjectName("saveSettingsButton")
        save_button.setDefault(True)
        button_box.addButton(save_button, QDialogButtonBox.ButtonRole.AcceptRole)

        cancel_button = QPushButton(tr("Cancel"))
        cancel_button.setObjectName("cancelSettingsButton")
        button_box.addButton(cancel_button, QDialogButtonBox.ButtonRole.RejectRole)

        footer = QWidget()
        footer.setObjectName("settingsDialogFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(26, 14, 26, 22)
        footer_layout.addWidget(self.error_area)
        footer_layout.addWidget(button_box)
        layout.addWidget(footer)

        save_button.clicked.connect(self._request_save)
        cancel_button.clicked.connect(self.reject)
        self.theme_input.currentIndexChanged.connect(self._request_theme_preview)
        self._initial_window_fit = InitialWindowFit(self, allowed=lambda: True)

    def _scroll_area(self, name: str) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName(name)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # InitialWindowFit can resize after reveal_control's first queued scroll.
        # Re-evaluate against the final range, not the pre-exposure geometry.
        scroll.verticalScrollBar().rangeChanged.connect(
            lambda _minimum, _maximum: QTimer.singleShot(0, self, self._ensure_current_focus_visible)
        )
        return scroll

    @staticmethod
    def _add_help(form: QFormLayout, text: str) -> None:
        hint = WrappingLabel(tr(text))
        hint.setObjectName("mutedText")
        form.addRow(hint)

    @property
    def settings_scroll_area(self) -> QScrollArea:
        """The active category's body, independent of hidden pages and their sizes."""
        return self.settings_tabs.currentWidget()

    def reveal_control(self, control: QWidget) -> None:
        for index in range(self.settings_tabs.count()):
            scroll = self.settings_tabs.widget(index)
            if scroll.isAncestorOf(control):
                self.settings_tabs.setCurrentIndex(index)
                control.setFocus(Qt.FocusReason.OtherFocusReason)
                QTimer.singleShot(
                    0,
                    self,
                    lambda scroll=scroll: (
                        scroll.ensureWidgetVisible(control, 0, 12) if control.isVisible() else None
                    ),
                )
                return

    def _first_invalid_control(self) -> QWidget | None:
        self._preference_error = None
        for control, choices in (
            (self.theme_input, ThemeId),
            (self.ssh_engine_input, SshEngine),
        ):
            if control.currentData() not in tuple(item.value for item in choices):
                return control
        family = self.terminal_font_family_input.currentData()
        if self.terminal_font_family_input.currentIndex() < 0 or (
            family is not None and type(family) is not str
        ):
            return self.terminal_font_family_input
        for control in (self.terminal_font_point_size_input, self.terminal_scrollback_lines_input):
            if not control.hasAcceptableInput():
                return control
        if type(self.backspace_input.currentData()) is not bool:
            return self.backspace_input
        try:
            _ = self.local_terminal_input.selected_settings
        except (ValueError, TypeError, OSError) as error:
            self._preference_error = str(error)
            return self.local_terminal_input.invalid_control
        return None

    @property
    def selected_settings(self) -> ApplicationSettings:
        """Build a candidate while preserving preferences this dialog does not edit."""
        if self._first_invalid_control() is not None:
            raise ValueError("Preferences contain an invalid selection or incomplete number")
        terminal_font = replace(
            self._initial_settings.terminal_font,
            family=self.terminal_font_family_input.currentData(),
            point_size=self.terminal_font_point_size_input.value(),
        )
        return replace(
            self._initial_settings,
            theme=ThemeId(self.theme_input.currentData()),
            terminal_font=terminal_font,
            terminal_scrollback_lines=self.terminal_scrollback_lines_input.value(),
            visual_bell_enabled=self.visual_bell_enabled_input.isChecked(),
            confirm_session_close=self.confirm_session_close_input.isChecked(),
            sftp_auto_open=self.sftp_auto_open_input.isChecked(),
            ssh_engine=SshEngine(self.ssh_engine_input.currentData()),
            terminal_highlighting=self._terminal_highlighting,
            local_terminal=self.local_terminal_input.selected_settings,
            backspace_sends_ctrl_h=self.backspace_input.currentData(),
            toolbar_actions=tuple(key for key, checkbox in self.toolbar_inputs.items()
                                  if checkbox.isChecked()),
        )

    def _open_highlighting(self) -> None:
        dialog = TerminalHighlightingDialog(self._terminal_highlighting, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._terminal_highlighting = dialog.selected_settings
        finally:
            schedule_delete_later(dialog)

    def show_save_error(self, message: str) -> None:
        """Keep the selected candidate visible so the user can retry."""
        self.error_area.show_message(message)
        self.error_label.setAccessibleName(tr("Settings error: {p0}", p0=f"{message}"))
        self.error_label.show()
        QAccessible.updateAccessibility(QAccessibleEvent(self.error_label, QAccessible.Event.Alert))
        # A persistence error concerns the whole draft. Keep the user's current
        # category/focus; the pinned footer makes the message visible directly.
        QTimer.singleShot(0, self, self._ensure_current_focus_visible)

    def _ensure_current_focus_visible(self) -> None:
        focused = self.focusWidget()
        scroll = self.settings_scroll_area
        if focused is not None and scroll.isAncestorOf(focused):
            scroll.ensureWidgetVisible(focused, 0, 12)

    def _request_save(self) -> None:
        """Ask the coordinator to persist and apply the selected snapshot."""
        self.error_label.hide()
        self.error_area.hide()
        self.error_label.setAccessibleName("")
        try:
            candidate = self.selected_settings
        except (TypeError, ValueError):
            self.show_save_error(
                tr(self._preference_error) if self._preference_error
                else tr("Check the selected preference values before saving.")
            )
            if (control := self._first_invalid_control()) is not None:
                self.reveal_control(control)
            return
        self.save_requested.emit(candidate)

    def _request_theme_preview(self) -> None:
        """Request a color-only preview; the owner controls persistence/rollback."""
        try:
            theme = ThemeId(self.theme_input.currentData())
        except (TypeError, ValueError):
            return  # Corrupt metadata must not escape a Qt signal or recolor.
        self.theme_preview_requested.emit(theme)
