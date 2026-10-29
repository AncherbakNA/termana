"""Presentation-only editors for the saved multiline command library."""

from __future__ import annotations

import unicodedata
from itertools import pairwise
from uuid import UUID, uuid4

from PySide6.QtCore import QEvent, QPoint, QSignalBlocker, Qt, QTimer, Signal, SignalInstance
from PySide6.QtGui import (
    QAccessible,
    QAccessibleEvent,
    QFont,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from termana.i18n import tr, validation_text
from termana.models.command_snippet import (
    MAX_COMMAND_SNIPPETS,
    MAX_SNIPPET_COMMAND_CHARACTERS,
    MAX_SNIPPET_NAME_CHARACTERS,
    CommandSnippet,
    validate_command_snippets,
)
from termana.ui_text import plain_text_tooltip
from termana.ui_typography import scale_widget_font
from termana.widgets.clipboard_text import copy_plain_text
from termana.widgets.form_controls import FormComboBox
from termana.widgets.numbered_plain_text_edit import NumberedPlainTextEdit
from termana.widgets.themed_checkbox import ThemedCheckBox
from termana.widgets.wrapping_label import WrappingLabel

_MAX_ERROR_CHARACTERS = 500
_BUTTON_TEXT_VERTICAL_CLEARANCE = 4
_LIBRARY_SCREEN_HORIZONTAL_MARGIN = 48
_LIBRARY_SCREEN_VERTICAL_MARGIN = 80
COMMAND_EDITOR_PREFERRED_HEIGHT = 640
COMMAND_EDITOR_MINIMUM_TEXT_HEIGHT = 100
_STORAGE_NOTICE = "Commands are stored as plain text. Never save passwords or tokens."


def _label(text: str, object_name: str = "mutedText") -> QLabel:
    label = WrappingLabel(text)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return label


def _button(text: str, name: str, role: str = "secondary") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(name)
    button.setProperty("actionRole", role)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button


def _safe_error(message: str) -> str:
    if not isinstance(message, str):
        raise TypeError("Command library error must be text")
    return "".join(
        "\N{REPLACEMENT CHARACTER}"
        if unicodedata.category(character).startswith("C") and character not in {"\n", "\t"}
        else character
        for character in message[:_MAX_ERROR_CHARACTERS]
    )


def _filter_key(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


class _ExplicitSaveButtonBox(QDialogButtonBox):
    """Use native commit ordering without Qt selecting an implicit Save default."""

    def showEvent(self, event):
        super().showEvent(event)
        for button in self.buttons():
            if not button.autoDefault():
                button.setDefault(False)


class _LibraryDialog(QDialog):
    """Keep a resizable, scrolling body separate from reachable footer actions."""

    def _fit_screen(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        width = max(1, available.width() - _LIBRARY_SCREEN_HORIZONTAL_MARGIN)
        height = max(1, available.height() - _LIBRARY_SCREEN_VERTICAL_MARGIN)
        self.setMinimumWidth(min(self.minimumWidth(), width))
        self.setMaximumSize(width, height)

    def showEvent(self, event) -> None:
        self._fit_screen()
        super().showEvent(event)

    def _build_body(self, title: str) -> QVBoxLayout:
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.setModal(True)
        self.setSizeGripEnabled(True)
        self._fit_screen()
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("settingsScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("settingsScrollContent")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(26, 24, 26, 18)
        layout.setSpacing(12)
        self.title_label = _label(title, "dialogTitle")
        scale_widget_font(self.title_label, 1.7, weight=QFont.Weight.Bold)
        layout.addWidget(self.title_label)
        self.scroll_area.setWidget(body)
        self._root_layout.addWidget(self.scroll_area, 1)
        return layout

    def _add_footer(self, button_box: QDialogButtonBox) -> None:
        self.footer = QWidget()
        self.footer.setObjectName("settingsDialogFooter")
        layout = QVBoxLayout(self.footer)
        layout.setContentsMargins(26, 14, 26, 22)
        layout.addWidget(button_box)
        self._root_layout.addWidget(self.footer)

    def _add_error(self, layout: QVBoxLayout) -> None:
        self.error_label = _label("", "settingsError")
        self.error_label.setAccessibleName(tr("Saved command error"))
        self.error_label.hide()
        layout.addWidget(self.error_label)

    def _show_error(self, message: str, *, announce: bool = True) -> None:
        message = _safe_error(message)
        self.error_label.setText(message)
        self.error_label.setAccessibleName(tr("Saved command error: {p0}", p0=f"{message}"))
        self.error_label.setVisible(bool(message))
        if message and announce:
            QAccessible.updateAccessibility(
                QAccessibleEvent(self.error_label, QAccessible.Event.Alert)
            )
            QTimer.singleShot(0, self, self._reveal_error)

    def _reveal_error(self) -> None:
        if self.error_label.isVisible():
            self.scroll_area.ensureWidgetVisible(self.error_label)

    def _reveal_widget(self, widget: QWidget) -> None:
        """Reveal the whole field rather than only the editor's caret rectangle."""
        self._reveal_widget_now(widget)
        # A pending wrapped-text/layout update can change the scroll range after
        # the first request. Recheck once in settled geometry, never move focus.
        QTimer.singleShot(0, self, lambda: self._reveal_widget_now(widget))

    def _reveal_widget_now(self, widget: QWidget) -> None:
        if not isValid(widget) or not widget.isVisibleTo(self):
            return
        point = widget.mapTo(self.scroll_area.widget(), QPoint())
        # A one-pixel margin keeps QRect's inclusive bottom/right edge inside.
        self.scroll_area.ensureVisible(point.x(), point.y(), 1, 1)
        self.scroll_area.ensureVisible(
            point.x() + widget.width() - 1, point.y() + widget.height() - 1, 1, 1
        )


class CommandSnippetDialog(_LibraryDialog):
    """Validate one draft and let the owner accept only after persistence succeeds."""

    save_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        snippet: CommandSnippet | None = None,
        duplicate: bool = False,
    ) -> None:
        super().__init__(parent)
        if snippet is not None and not isinstance(snippet, CommandSnippet):
            raise TypeError("Saved command must be a CommandSnippet")
        if type(duplicate) is not bool or (duplicate and snippet is None):
            raise ValueError("Duplicating requires a saved-command draft")
        self._snippet_id = snippet.snippet_id if snippet is not None else uuid4()
        self._favorite = snippet.favorite if snippet is not None else False
        self._geometry_restore_pending = False
        self._initial_geometry_fitted = False
        self._last_command = ""
        self._oversized_change = False
        self._candidate: CommandSnippet | None = None
        self.setObjectName("commandSnippetDialog")
        self.setMinimumWidth(500)
        self.resize(620, COMMAND_EDITOR_PREFERRED_HEIGHT)
        self.setAccessibleDescription(tr("Save named command lines without executing them."))
        body = self._build_body(
            tr("Duplicate saved command")
            if duplicate
            else tr("Edit saved command")
            if snippet is not None
            else tr("New saved command")
        )
        body.addWidget(_label(tr("Give a frequently used command a name so it is easy to find.")))

        self.name_input = QLineEdit()
        self.name_input.setObjectName("commandSnippetNameInput")
        self.name_input.setAccessibleName(tr("Saved command name"))
        self.name_input.setMaxLength(2 * (MAX_SNIPPET_NAME_CHARACTERS + 1))
        self.name_input.setPlaceholderText(tr("Show disk usage"))
        name_label = _label(tr("Name"), "settingsSectionTitle")
        name_label.setBuddy(self.name_input)
        body.addWidget(name_label)
        body.addWidget(self.name_input)

        self.command_input = NumberedPlainTextEdit()
        self.command_input.setObjectName("commandSnippetCommandInput")
        self.command_input.setAccessibleName(tr("Command lines"))
        self.command_input.setAccessibleDescription(
            tr(
                "Enter command lines, up to {p0} characters. Line breaks send Enter, including a final line break. Spaces are preserved.",
                p0=f"{MAX_SNIPPET_COMMAND_CHARACTERS:,}",
            )
        )
        self.command_input.setPlaceholderText("df -h")
        self.command_input.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.command_input.setTabChangesFocus(True)
        # Use the available body space, not QAbstractScrollArea's oversized hint.
        self.command_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.command_input.setMinimumHeight(COMMAND_EDITOR_MINIMUM_TEXT_HEIGHT)
        self.command_input.installEventFilter(self)
        command_label = _label(tr("Command"), "settingsSectionTitle")
        command_label.setBuddy(self.command_input)
        body.addWidget(command_label)
        body.addWidget(self.command_input, 1)
        body.addWidget(
            _label(
                tr(
                    "Up to {p0} characters. Saving runs nothing. Inserting line breaks may run commands; a final line break runs the last command too.",
                    p0=f"{MAX_SNIPPET_COMMAND_CHARACTERS:,}",
                )
            )
        )
        self.storage_notice = _label(tr(_STORAGE_NOTICE))
        self.template_mode = ThemedCheckBox(tr("Use parameters"))
        self.template_mode.setChecked(snippet.template if snippet is not None else False)
        self.template_mode.setAccessibleName(tr("Use command parameters"))
        body.addWidget(self.template_mode)
        body.addWidget(
            _label(
                tr(
                    "Enable {{name}} placeholders to ask for parameters before inserting. Parameters are plain text, not shell-quoted. Values are not saved. Review the complete command before inserting it."
                )
            )
        )
        body.addWidget(self.storage_notice)
        self._add_error(body)

        self.button_box = _ExplicitSaveButtonBox()
        self.save_button = _button(tr("Save"), "saveCommandSnippetButton", "primary")
        self.save_button.setAccessibleName(tr("Save command"))
        self.cancel_button = _button(tr("Cancel"), "cancelCommandSnippetButton")
        self.button_box.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.button_box.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        self._add_footer(self.button_box)
        self.save_button.clicked.connect(self._request_save)
        self.cancel_button.clicked.connect(self.reject)
        self.name_input.textChanged.connect(self._validate)
        self.command_input.textChanged.connect(self._command_changed)
        self.template_mode.toggled.connect(self._validate)
        QWidget.setTabOrder(self.name_input, self.command_input)
        QWidget.setTabOrder(self.command_input, self.save_button)
        QWidget.setTabOrder(self.save_button, self.cancel_button)

        if snippet is not None:
            self.name_input.setText(snippet.name)
            self.command_input.setPlainText(snippet.command)
            self.name_input.selectAll()
        self._validate()
        if snippet is None:
            self._show_error("", announce=False)
        self.name_input.setFocus()

    def showEvent(self, event):
        super().showEvent(event)
        # QSS polish can replace constructor-time minimums. Restore the useful
        # edit area after polish; the scroll body, not the pinned footer, yields.
        self._queue_text_geometry_restore()

    def eventFilter(self, watched, event):
        if watched is self.command_input and event.type() in (
            QEvent.Type.StyleChange,
            QEvent.Type.FontChange,
        ):
            self._queue_text_geometry_restore()
        return super().eventFilter(watched, event)

    def _queue_text_geometry_restore(self):
        if not self._geometry_restore_pending:
            self._geometry_restore_pending = True
            QTimer.singleShot(0, self, self._restore_text_geometry)

    def _restore_text_geometry(self):
        self._geometry_restore_pending = False
        self.command_input.setMinimumHeight(COMMAND_EDITOR_MINIMUM_TEXT_HEIGHT)
        if self.isVisible() and not self._initial_geometry_fitted:
            self.scroll_area.widget().layout().activate()
            required = (
                self.scroll_area.widget().minimumSizeHint().height()
                + self.footer.minimumSizeHint().height()
            )
            # Auto-fit only the initial default, not a caller/user-requested
            # short window; the latter intentionally uses vertical scrolling.
            if self.height() == min(COMMAND_EDITOR_PREFERRED_HEIGHT, self.maximumHeight()):
                self.resize(self.width(), min(max(self.height(), required), self.maximumHeight()))
            self._initial_geometry_fitted = True

    def show_save_error(self, message: str) -> None:
        """Retain the exact draft and identity after a failed save so it can be retried."""
        self._show_error(message)

    def _command_changed(self) -> None:
        # QTextDocument.toPlainText() silently converts nonbreaking spaces to spaces.
        # Raw text preserves the command; its paragraph separators remain invalid.
        command = self.command_input.document().toRawText().replace("\u2029", "\n")
        self._oversized_change = len(command) > MAX_SNIPPET_COMMAND_CHARACTERS
        if self._oversized_change:
            # Reject the whole oversized edit, never offer a truncated command for saving.
            position = self.command_input.textCursor().position()
            with QSignalBlocker(self.command_input):
                self.command_input.setUndoRedoEnabled(False)
                self.command_input.setPlainText(self._last_command)
                cursor = self.command_input.textCursor()
                cursor.setPosition(
                    min(position, self.command_input.document().characterCount() - 1)
                )
                self.command_input.setTextCursor(cursor)
                self.command_input.setUndoRedoEnabled(True)
        else:
            self._last_command = command
        self._validate()

    def _validate(self) -> None:
        self._candidate = None
        message = ""
        if self._oversized_change:
            message = tr(
                "The command limit is {p0} characters. The oversized change was not kept. Edit the command to continue.",
                p0=f"{MAX_SNIPPET_COMMAND_CHARACTERS:,}",
            )
        else:
            try:
                self._candidate = CommandSnippet(
                    name=self.name_input.text(),
                    command=self.command_input.document().toRawText().replace("\u2029", "\n"),
                    snippet_id=self._snippet_id,
                    template=self.template_mode.isChecked(),
                    favorite=self._favorite,
                )
            except (TypeError, ValueError) as error:
                message = validation_text(str(error))
        self.save_button.setEnabled(self._candidate is not None)
        self._show_error(message, announce=False)

    def _request_save(self) -> None:
        self._validate()
        if self._candidate is not None:
            self.save_requested.emit(self._candidate)


class SavedCommandsDialog(_LibraryDialog):
    """Browse named commands and request edits or insertion without executing anything."""

    new_requested = Signal()
    edit_requested = Signal(object)
    duplicate_requested = Signal(object)
    delete_requested = Signal(object)
    insert_requested = Signal(object)
    favorite_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        snippets: tuple[CommandSnippet, ...],
        target_label: str | None = None,
    ) -> None:
        super().__init__(parent)
        if target_label is not None and not isinstance(target_label, str):
            raise TypeError("Insertion target label must be text")
        self._snippets: tuple[CommandSnippet, ...] = ()
        self._selection_id: UUID | None = None
        self._has_insertion_target = target_label is not None
        self._insertion_available = self._has_insertion_target
        self._editing_available = True
        self._button_reflow_pending = False
        self.setObjectName("savedCommandsDialog")
        self.setMinimumWidth(600)
        self.resize(720, 760)
        self.setAccessibleDescription(
            tr("Manage saved commands. Multiline insertion may run earlier lines.")
        )
        body = self._build_body(tr("Saved commands"))
        self.storage_notice = _label(tr(_STORAGE_NOTICE))
        body.addWidget(self.storage_notice)
        self.target_label = _label(
            tr("Insert into: {p0}", p0=f"{target_label}")
            if target_label is not None
            else tr("Open a terminal to insert a command")
        )
        self.target_label.setAccessibleName(tr("Insertion target"))
        body.addWidget(self.target_label)
        body.addWidget(_label(tr("Every line break sends Enter. A final line break runs the last command too.")))

        self.filter_input = QLineEdit()
        self.filter_input.setObjectName("savedCommandFilterInput")
        self.filter_input.setPlaceholderText(tr("Search names and command text"))
        self.filter_input.setAccessibleName(tr("Search saved commands"))
        self.filter_input.setAccessibleDescription(
            tr(
                "Filter by name or command text. Up and Down select a command; Enter opens its preview without inserting it."
            )
        )
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.setMaxLength(MAX_SNIPPET_NAME_CHARACTERS)
        body.addWidget(self.filter_input)

        filter_options = QHBoxLayout()
        self.search_scope = FormComboBox()
        self.search_scope.setAccessibleName(tr("Saved command search scope"))
        self.search_scope.addItems((tr("Name + text"), tr("Name only"), tr("Text only")))
        self.sort_order = FormComboBox()
        self.sort_order.setAccessibleName(tr("Saved command sort order"))
        self.sort_order.addItems((tr("Name: A-Z"), tr("Name: Z-A")))
        for combo in (self.search_scope, self.sort_order):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            filter_options.addWidget(combo, 1)
        body.addLayout(filter_options)
        self.favorites_only = ThemedCheckBox(tr("Only favorites"))
        self.favorites_only.setAccessibleName(tr("Show favorite saved commands only"))
        body.addWidget(self.favorites_only)

        self.snippets_list = QListWidget()
        self.snippets_list.setObjectName("savedCommandsList")
        self.snippets_list.setAccessibleName(tr("Saved commands"))
        self.snippets_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.snippets_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.snippets_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.snippets_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.snippets_list.setMinimumHeight(130)
        body.addWidget(self.snippets_list, 2)
        body.addWidget(
            _label(tr("↑ / ↓ to choose · Enter to preview · Insert is a separate action"))
        )
        self.state_label = _label("", "mutedText")
        body.addWidget(self.state_label)
        self.count_label = _label("")
        body.addWidget(self.count_label)

        management_buttons = QDialogButtonBox()
        self.management_buttons = management_buttons
        management_buttons.setOrientation(Qt.Orientation.Vertical)
        self.new_button = _button(tr("New…"), "newCommandSnippetButton")
        self.edit_button = _button(tr("Edit…"), "editCommandSnippetButton")
        self.duplicate_button = _button(tr("Duplicate…"), "duplicateCommandSnippetButton")
        self.duplicate_button.setAccessibleName(tr("Duplicate selected saved command"))
        self.delete_button = _button(tr("Delete…"), "deleteCommandSnippetButton", "danger")
        self.favorite_button = _button(tr("Add to favorites"), "favoriteCommandSnippetButton")
        self.new_button.setAccessibleName(tr("Create a saved command"))
        self.edit_button.setAccessibleName(tr("Edit selected saved command"))
        self.delete_button.setAccessibleName(tr("Delete selected saved command"))
        for button in (
            self.new_button,
            self.edit_button,
            self.duplicate_button,
            self.favorite_button,
            self.delete_button,
        ):
            management_buttons.addButton(button, QDialogButtonBox.ButtonRole.ActionRole)
        body.addWidget(management_buttons)

        preview_label = _label(tr("Command preview"), "settingsSectionTitle")
        self.command_preview = NumberedPlainTextEdit()
        self.command_preview.setObjectName("savedCommandPreview")
        self.command_preview.setAccessibleName(tr("Selected command preview"))
        self.command_preview.setAccessibleDescription(
            tr("Read-only command text; selecting does not run it.")
        )
        self.command_preview.setReadOnly(True)
        self.command_preview.setTabChangesFocus(True)
        self.command_preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.command_preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.command_preview.setMinimumHeight(90)
        preview_label.setBuddy(self.command_preview)
        body.addWidget(preview_label)
        body.addWidget(self.command_preview, 1)
        self._add_error(body)

        self.button_box = QDialogButtonBox()
        self.button_box.setOrientation(Qt.Orientation.Vertical)
        self.insert_button = _button(
            tr("Insert"), "insertCommandSnippetButton", "primary"
        )
        self.insert_button.setAccessibleName(tr("Insert selected command; line breaks send Enter"))
        self.copy_button = _button(tr("Copy"), "copySavedCommandButton")
        self.copy_button.setAccessibleName(tr("Copy selected command to clipboard"))
        self.copy_button.setToolTip(tr("Copy the complete command. Nothing is sent to a terminal."))
        self.close_button = _button(tr("Close"), "closeSavedCommandsButton")
        self.button_box.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.button_box.addButton(self.insert_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.button_box.addButton(self.close_button, QDialogButtonBox.ButtonRole.RejectRole)
        self._add_footer(self.button_box)
        self.filter_input.textChanged.connect(self._apply_filter)
        self.search_scope.currentIndexChanged.connect(self._apply_filter)
        self.sort_order.currentIndexChanged.connect(lambda: self.set_snippets(self._snippets))
        self.favorites_only.toggled.connect(self._apply_filter)
        self.snippets_list.itemSelectionChanged.connect(self._selection_changed)
        self.new_button.clicked.connect(self._request_new)
        self.edit_button.clicked.connect(
            lambda: self._request_selected(self.edit_button, self.edit_requested)
        )
        self.duplicate_button.clicked.connect(
            lambda: self._request_selected(self.duplicate_button, self.duplicate_requested)
        )
        self.delete_button.clicked.connect(
            lambda: self._request_selected(self.delete_button, self.delete_requested)
        )
        self.favorite_button.clicked.connect(
            lambda: self._request_selected(self.favorite_button, self.favorite_requested)
        )
        self.insert_button.clicked.connect(
            lambda: self._request_selected(self.insert_button, self.insert_requested)
        )
        self.close_button.clicked.connect(self.reject)
        self.copy_button.clicked.connect(self._copy_selected)
        self.snippets_list.itemDoubleClicked.connect(lambda *_args: self._focus_preview())
        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self.find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.find_shortcut.activated.connect(self._focus_filter)
        tab_order = (
            self.filter_input,
            self.search_scope,
            self.sort_order,
            self.favorites_only,
            self.snippets_list,
            self.new_button,
            self.edit_button,
            self.duplicate_button,
            self.favorite_button,
            self.delete_button,
            self.command_preview,
            self.copy_button,
            self.insert_button,
            self.close_button,
        )
        for previous, following in pairwise(tab_order):
            QWidget.setTabOrder(previous, following)
        self.set_snippets(snippets)
        self.filter_input.setFocus()
        self.filter_input.installEventFilter(self)
        self.snippets_list.installEventFilter(self)
        self.management_buttons.installEventFilter(self)
        self.button_box.installEventFilter(self)
        self.scroll_area.viewport().installEventFilter(self)

    def eventFilter(self, watched, event: QEvent) -> bool:
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.LayoutRequest,
        ) and watched in (self.management_buttons, self.button_box, self.scroll_area.viewport()):
            if not self._button_reflow_pending:
                self._button_reflow_pending = True
                QTimer.singleShot(0, self, self._reflow_buttons)
        if (
            event.type() == QEvent.Type.KeyPress
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            if watched in (self.filter_input, self.snippets_list):
                if event.key() == Qt.Key.Key_Escape and self.filter_input.text():
                    self.filter_input.clear()
                    self._focus_filter()
                    return True
                if event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                    page_size = max(
                        1,
                        self.snippets_list.viewport().height()
                        // max(1, self.snippets_list.sizeHintForRow(0)),
                    )
                    self._move_selection(
                        -page_size if event.key() == Qt.Key.Key_PageUp else page_size
                    )
                    return True
            if watched is self.filter_input and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                self._move_selection(-1 if event.key() == Qt.Key.Key_Up else 1)
                return True
            if watched in (self.filter_input, self.snippets_list) and event.key() in (
                Qt.Key.Key_Return,
                Qt.Key.Key_Enter,
            ):
                self._focus_preview()
                return True
        return super().eventFilter(watched, event)

    def _reflow_buttons(self) -> None:
        self._button_reflow_pending = False
        if not self.isVisible():
            return
        # App styles can override constructor-time minimums during polish.
        # Preserve useful preview/list areas after that pass, letting the body scroll.
        self.command_preview.setMinimumHeight(90)
        self.snippets_list.setMinimumHeight(130)
        margins = self.scroll_area.widget().layout().contentsMargins()
        footer_margins = self.footer.layout().contentsMargins()
        for box, available in (
            (
                self.management_buttons,
                self.scroll_area.viewport().width() - margins.left() - margins.right(),
            ),
            (self.button_box, self.footer.width() - footer_margins.left() - footer_margins.right()),
        ):
            buttons = box.buttons()
            for button in buttons:
                # A native button-box layout can compress a large Windows font
                # below its text height. Reapply the bound after style polish.
                # Preserve its ordinary compact layout instead of forcing the
                # larger native size hint into every narrow action row.
                button.setMinimumHeight(
                    button.fontMetrics().height() + _BUTTON_TEXT_VERTICAL_CLEARANCE
                )
            required = sum(
                max(button.sizeHint().width(), button.minimumWidth()) for button in buttons
            )
            # QDialogButtonBox uses native per-control spacing (14px on Cocoa),
            # even when its internal layout reports spacing()==0. Respect that
            # style metric rather than selecting a row which Qt cannot fit.
            native_spacing = box.style().layoutSpacing(
                QSizePolicy.ControlType.PushButton,
                QSizePolicy.ControlType.PushButton,
                Qt.Orientation.Horizontal,
                None,
                box,
            )
            required += max(0, box.layout().spacing(), native_spacing) * (len(buttons) - 1)
            box_margins = box.layout().contentsMargins()
            required += box_margins.left() + box_margins.right()
            orientation = (
                Qt.Orientation.Vertical if required > available else Qt.Orientation.Horizontal
            )
            if box.orientation() != orientation:
                box.setOrientation(orientation)

    def _move_selection(self, step: int) -> None:
        """Navigate visible rows only, without implicitly choosing during filtering."""
        items = [
            self.snippets_list.item(index)
            for index in range(self.snippets_list.count())
            if not self.snippets_list.item(index).isHidden()
        ]
        if not items:
            return
        selected = self.snippets_list.selectedItems()
        current = selected[0] if selected else None
        if current in items:
            index = max(0, min(len(items) - 1, items.index(current) + step))
        else:
            index = 0 if step > 0 else len(items) - 1
        self.snippets_list.setCurrentItem(items[index])
        self.snippets_list.scrollToItem(items[index])

    def _focus_preview(self) -> None:
        if self._selected_snippet() is None:
            self._move_selection(1)
        if self._selected_snippet() is not None:
            self.command_preview.setFocus(Qt.FocusReason.OtherFocusReason)
            self._reveal_widget(self.command_preview)

    def _focus_filter(self) -> None:
        self.filter_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.filter_input.selectAll()
        self._reveal_widget(self.filter_input)

    @property
    def snippets(self) -> tuple[CommandSnippet, ...]:
        """Return the currently presented immutable snapshot."""
        return self._snippets

    def set_snippets(
        self,
        snippets: tuple[CommandSnippet, ...],
        select_id: UUID | None = None,
    ) -> None:
        """Refresh after a committed operation, retaining selection by stable identity."""
        if not isinstance(snippets, tuple):
            raise TypeError("Saved commands must be a tuple")
        snippets = validate_command_snippets(snippets)
        ids = {snippet.snippet_id for snippet in snippets}
        self._snippets = tuple(
            sorted(
                snippets,
                key=lambda snippet: _filter_key(snippet.name),
                reverse=self.sort_order.currentIndex() == 1,
            )
        )
        self._search_documents = {
            snippet.snippet_id: _filter_key(f"{snippet.name}\n{snippet.command}")
            for snippet in self._snippets
        }
        self._name_search_documents = {
            snippet.snippet_id: _filter_key(snippet.name) for snippet in self._snippets
        }
        self._command_search_documents = {
            snippet.snippet_id: _filter_key(snippet.command) for snippet in self._snippets
        }
        requested_id = select_id if select_id is not None else self._selection_id
        self._selection_id = requested_id if requested_id in ids else None
        with QSignalBlocker(self.snippets_list):
            self.snippets_list.clear()
            for snippet in self._snippets:
                item = QListWidgetItem(("★ " if snippet.favorite else "") + snippet.name)
                item.setData(Qt.ItemDataRole.UserRole, snippet.snippet_id)
                item.setData(
                    Qt.ItemDataRole.AccessibleTextRole,
                    (tr("Favorite: ") if snippet.favorite else "") + snippet.name,
                )
                item.setToolTip(plain_text_tooltip(f"{snippet.name}\n{snippet.command}"))
                self.snippets_list.addItem(item)
        self._apply_filter()

    def set_insertion_available(self, available: bool) -> None:
        """Disable insertion if the owner's fixed target is no longer usable."""
        self._insertion_available = self._has_insertion_target and bool(available)
        self._update_actions()

    @property
    def selected_snippet_id(self) -> UUID | None:
        """Expose only the currently visible selected identity, never a hidden old row."""
        selected = self._selected_snippet()
        return selected.snippet_id if selected is not None else None

    def set_editing_available(self, available: bool) -> None:
        """Keep a failed or unsupported library read-only until its owner reloads it."""
        self._editing_available = bool(available)
        self._update_actions()

    def show_error(self, message: str) -> None:
        """Display a bounded, accessible plain-text error without changing the snapshot."""
        self._show_error(message)

    def _apply_filter(self) -> None:
        key = _filter_key(self.filter_input.text().strip())
        documents = (
            self._search_documents,
            self._name_search_documents,
            self._command_search_documents,
        )[self.search_scope.currentIndex()]
        for combo in (self.search_scope, self.sort_order):
            combo.setToolTip(plain_text_tooltip(combo.currentText()))
        visible_count = 0
        favorite_ids = {snippet.snippet_id for snippet in self._snippets if snippet.favorite}
        with QSignalBlocker(self.snippets_list):
            self.snippets_list.clearSelection()
            self.snippets_list.setCurrentItem(None)
            for index in range(self.snippets_list.count()):
                item = self.snippets_list.item(index)
                hidden = key not in documents[item.data(Qt.ItemDataRole.UserRole)]
                if self.favorites_only.isChecked():
                    hidden |= item.data(Qt.ItemDataRole.UserRole) not in favorite_ids
                item.setHidden(hidden)
                if not hidden:
                    visible_count += 1
                    if item.data(Qt.ItemDataRole.UserRole) == self._selection_id:
                        self.snippets_list.setCurrentItem(item)
        self.state_label.setText(
            tr("No saved commands yet. Choose New to add one.")
            if not self._snippets
            else tr("No saved commands match your search.")
            if not visible_count
            else ""
        )
        self.state_label.setVisible(not visible_count)
        self.count_label.setText(
            tr(
                "{p0} shown · {p1} of {p2} saved commands",
                p0=f"{visible_count}",
                p1=f"{len(self._snippets)}",
                p2=f"{MAX_COMMAND_SNIPPETS}",
            )
        )
        self._update_actions()

    def _selected_snippet(self) -> CommandSnippet | None:
        items = self.snippets_list.selectedItems()
        if not items or items[0].isHidden():
            return None
        selected_id = items[0].data(Qt.ItemDataRole.UserRole)
        return next((item for item in self._snippets if item.snippet_id == selected_id), None)

    def _selection_changed(self) -> None:
        selected = self._selected_snippet()
        self._selection_id = selected.snippet_id if selected is not None else None
        self._update_actions()

    def _update_actions(self) -> None:
        selected = self._selected_snippet()
        self.copy_button.setEnabled(selected is not None)
        self.copy_button.setText(tr("Copy"))
        self.copy_button.setAccessibleName(tr("Copy selected command to clipboard"))
        self.command_preview.setPlainText(selected.command if selected is not None else "")
        self.new_button.setEnabled(
            self._editing_available and len(self._snippets) < MAX_COMMAND_SNIPPETS
        )
        self.edit_button.setEnabled(self._editing_available and selected is not None)
        self.duplicate_button.setEnabled(
            self._editing_available
            and selected is not None
            and len(self._snippets) < MAX_COMMAND_SNIPPETS
        )
        self.delete_button.setEnabled(self._editing_available and selected is not None)
        self.favorite_button.setEnabled(self._editing_available and selected is not None)
        self.favorite_button.setText(
            tr("Remove favorite")
            if selected is not None and selected.favorite
            else tr("Add to favorites")
        )
        self.insert_button.setEnabled(self._insertion_available and selected is not None)
        self.insert_button.setText(
            tr("Parameters…")
            if selected is not None and selected.template
            else tr("Insert")
        )

    def _copy_selected(self) -> None:
        """Copy a currently visible, validated snapshot, never editable preview contents."""
        selected = self._selected_snippet()
        if selected is None or not self.copy_button.isEnabled():
            return
        copied = copy_plain_text(selected.command)
        self.copy_button.setText(tr("Copied") if copied else tr("Copy unavailable"))
        self.copy_button.setAccessibleName(
            tr("Command copied to clipboard")
            if copied
            else tr("Clipboard unavailable; command not copied")
        )
        QAccessible.updateAccessibility(
            QAccessibleEvent(self.copy_button, QAccessible.Event.NameChanged)
        )

    def _request_new(self) -> None:
        if self.new_button.isEnabled():
            self.new_requested.emit()

    def _request_selected(self, button: QPushButton, signal: SignalInstance) -> None:
        selected = self._selected_snippet()
        if selected is None or not button.isEnabled():
            return
        signal.emit(selected.snippet_id)
