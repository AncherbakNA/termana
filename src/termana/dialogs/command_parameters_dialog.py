"""An owned ephemeral parameter form with an exact, inert command preview."""

from PySide6.QtWidgets import QDialogButtonBox, QLineEdit, QPlainTextEdit

from termana.dialogs.command_snippet_dialogs import _button, _label, _LibraryDialog
from termana.i18n import tr, validation_text
from termana.models.command_snippet import CommandSnippet
from termana.models.command_template import (
    MAX_PARAMETER_CHARACTERS,
    command_parameters,
    render_command_template,
)


class CommandParametersDialog(_LibraryDialog):
    """Enter/Escape cancel; only an explicit Insert click accepts a validated snapshot."""

    def __init__(self, parent, *, snippet: CommandSnippet, target_label: str):
        super().__init__(parent)
        if not snippet.template:
            raise ValueError("Only explicit parameter commands need this form")
        self._snippet = snippet
        self.rendered_command = None
        self.setObjectName("commandParametersDialog")
        self.setMinimumWidth(500)
        self.resize(620, 600)
        body = self._build_body(tr("Command parameters"))
        self.target_label = _label(tr("Insert into: {p0}", p0=f"{target_label}"))
        body.addWidget(self.target_label)
        body.addWidget(
            _label(
                tr(
                    "Values are not saved. Substitution is literal: Termana does not shell-quote, expand variables or evaluate expressions. Review the complete command."
                )
            )
        )
        self.inputs = {}
        for name in command_parameters(snippet.command):
            field = QLineEdit()
            field.setMaxLength(2 * (MAX_PARAMETER_CHARACTERS + 1))
            field.setAccessibleName(tr("Command parameter {p0}", p0=f"{name}"))
            label = _label(name)
            label.setBuddy(field)
            body.addWidget(label)
            body.addWidget(field)
            self.inputs[name] = field
            field.textChanged.connect(self._refresh)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setTabChangesFocus(True)
        self.preview.setMinimumHeight(100)
        self.preview.setAccessibleName(tr("Exact expanded command preview"))
        body.addWidget(self.preview)
        body.addWidget(
            _label(tr("Insert sends text only, without Enter. Nothing has been sent yet."))
        )
        self._add_error(body)
        box = QDialogButtonBox()
        self.insert_button = _button(
            tr("Insert without Enter"), "insertParameterizedCommand", "primary"
        )
        self.cancel_button = _button(tr("Cancel"), "cancelCommandParameters")
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setDefault(True)
        box.addButton(self.insert_button, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        self._add_footer(box)
        self.insert_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self._refresh()
        next(iter(self.inputs.values())).setFocus()

    def _render(self):
        return render_command_template(
            self._snippet.command, {name: field.text() for name, field in self.inputs.items()}
        )

    def _refresh(self):
        try:
            rendered = self._render()
        except (ValueError, TypeError) as error:
            self.preview.clear()
            self._show_error(validation_text(str(error)), announce=False)
            self.insert_button.setEnabled(False)
        else:
            self.preview.setPlainText(rendered)
            self._show_error("", announce=False)
            self.insert_button.setEnabled(True)

    def accept(self):
        try:
            self.rendered_command = self._render()
        except (ValueError, TypeError):
            self._refresh()
            return
        super().accept()

    def clear_values(self):
        """Release UI references after use; this does not promise memory zeroization."""
        for field in self.inputs.values():
            field.clear()
        self.preview.clear()
        self.rendered_command = None
