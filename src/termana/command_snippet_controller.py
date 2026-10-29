"""Transactional saved-command management and identity-bound terminal insertion."""

from collections.abc import Callable, Iterable
from dataclasses import replace
from uuid import UUID

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QDialog, QWidget
from shiboken6 import isValid

from termana.dialogs.action_dialog import ActionDialog
from termana.dialogs.command_parameters_dialog import CommandParametersDialog
from termana.dialogs.command_snippet_dialogs import CommandSnippetDialog, SavedCommandsDialog
from termana.i18n import tr, validation_text
from termana.models.command_snippet import (
    CommandSnippet,
    duplicate_command_snippet,
    validate_command_snippets,
)
from termana.qt_lifecycle import schedule_delete_later
from termana.storage.command_snippet_store import CommandSnippetStore, CommandSnippetStoreError
from termana.widgets.terminal_workspace import TerminalWorkspace


class CommandSnippetController(QObject):
    """Persist explicit changes before updating the visible library."""

    message = Signal(str)
    library_changed = Signal(object)

    def __init__(
        self,
        window: QWidget,
        workspace: TerminalWorkspace,
        store: CommandSnippetStore | None = None,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._workspace = workspace
        self._store = store
        self._snippets: tuple[CommandSnippet, ...] = ()
        self._loaded = False
        self._writable = True
        self._closing = False
        self._inserting = False
        self._favoriting = False
        self._dialog: SavedCommandsDialog | None = None
        self._editor: CommandSnippetDialog | None = None
        self._confirmation: ActionDialog | None = None
        self._parameters: CommandParametersDialog | None = None
        self._target_id: UUID | None = None
        self._target_label: str | None = None

    def stop(self) -> None:
        """Dismiss outstanding editors before the application closes its sessions."""
        self._closing = True
        for dialog in (self._parameters, self._confirmation, self._editor, self._dialog):
            if dialog is not None and isValid(dialog):
                dialog.reject()

    def sidebar_snapshot(self) -> tuple[tuple[CommandSnippet, ...], str | None]:
        """Load the existing library lazily, without selecting or writing a terminal."""
        error = None
        if not self._loaded and not self._closing:
            try:
                self._snippets = self._store.load() if self._store is not None else ()
            except CommandSnippetStoreError:
                self._writable = False
                error = tr(
                    "Saved commands could not be loaded. The file was left unchanged. Repair it and reopen this window to retry."
                )
            else:
                self._loaded = True
                self._writable = True
        return self._snippets, error

    def open_dialog(
        self, requested_session_id: object = None, *, select_snippet_id: UUID | None = None
    ) -> None:
        if self._closing:
            return
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        _, error = self.sidebar_snapshot()
        target = self._workspace.current_command_target()
        if isinstance(requested_session_id, UUID) and (
            target is None or target[0] != requested_session_id
        ):
            target = None
        self._target_id = target[0] if target else None
        self._target_label = target[1] if target else None
        dialog = SavedCommandsDialog(
            self._window, snippets=self._snippets, target_label=target[1] if target else None
        )
        self._dialog = dialog
        if select_snippet_id is not None:
            dialog.set_snippets(self._snippets, select_id=select_snippet_id)
        dialog.set_editing_available(self._writable)
        dialog.set_insertion_available(self._target_available())
        if error:
            dialog.show_error(error)
        dialog.new_requested.connect(lambda: self._edit(None))
        dialog.edit_requested.connect(self._edit)
        dialog.duplicate_requested.connect(
            lambda snippet_id: self._edit(snippet_id, duplicate=True)
        )
        dialog.delete_requested.connect(self._delete)
        dialog.insert_requested.connect(self._insert)
        dialog.favorite_requested.connect(self._toggle_favorite)
        try:
            dialog.exec()
        finally:
            self._dialog = None
            self._target_id = None
            self._target_label = None
            schedule_delete_later(dialog)

    def _find(self, snippet_id: object) -> CommandSnippet | None:
        return next((s for s in self._snippets if s.snippet_id == snippet_id), None)

    def insert_from_sidebar(self, snippet_id: object) -> bool:
        """Insert into the exact current ready owner, never submit or retarget it."""
        if self._closing or self._inserting or self._dialog is not None or self._favoriting:
            return False
        snippet = self._find(snippet_id)
        target = self._workspace.current_command_target()
        if snippet is None or target is None:
            self.message.emit(tr("Select a ready terminal and a saved command before inserting."))
            return False
        target_id, target_label = target
        owner = self._workspace.terminal_session_generation(target_id)

        def available():
            return (
                not self._closing
                and self._dialog is None
                and owner is not None
                and self._workspace.terminal_session_generation(target_id) is owner
                and self._workspace.command_insertion_available(target_id)
            )

        self._inserting = True
        try:
            command = self._command_for_insertion(
                snippet,
                parent=self._window,
                target_label=target_label,
                available=available,
                show_error=self.message.emit,
            )
            if command is None:
                return False
            inserted = available() and self._workspace.insert_saved_command(target_id, command)
        finally:
            self._inserting = False
        if not inserted:
            self.message.emit(tr("The original terminal is not ready for command parameters."))
            return False
        self.message.emit(
            tr("Command inserted; Enter was sent only for line breaks in the command.")
        )
        QTimer.singleShot(0, self, lambda: self._workspace.focus_command_target(target_id))
        return True

    def _command_for_insertion(
        self,
        snippet: CommandSnippet,
        *,
        parent: QWidget,
        target_label: str,
        available: Callable[[], bool],
        show_error: Callable[[str], None],
    ) -> str | None:
        """Share parameter consent and snapshot guards across library and sidebar."""
        if not snippet.template:
            return snippet.command
        if not available():
            show_error(tr("The original terminal is not ready for command parameters."))
            return None
        parameters = CommandParametersDialog(parent, snippet=snippet, target_label=target_label)
        self._parameters = parameters
        try:
            accepted = parameters.exec() == QDialog.DialogCode.Accepted
            command = parameters.rendered_command if accepted and isValid(parameters) else None
        finally:
            self._parameters = None
            if isValid(parameters):
                parameters.clear_values()
                schedule_delete_later(parameters)
        if not accepted or command is None:
            return None
        if not available():
            if not self._closing and isValid(parent):
                show_error(tr("The original terminal is not ready for command parameters."))
            return None
        if self._find(snippet.snippet_id) != snippet:
            show_error(tr("This saved command changed. Reopen it before inserting."))
            return None
        return command

    def _active(self) -> bool:
        return not self._closing and self._dialog is not None and isValid(self._dialog)

    def _edit(self, snippet_id: object, *, duplicate: bool = False) -> None:
        if not self._active() or not self._writable or self._editor is not None or self._favoriting:
            return
        original = self._find(snippet_id)
        if snippet_id is not None and original is None:
            return
        draft = original
        if duplicate:
            try:
                draft = duplicate_command_snippet(original, self._snippets)
            except ValueError as error:
                self._dialog.show_error(validation_text(str(error)))
                return
        editor = CommandSnippetDialog(self._dialog, snippet=draft, duplicate=duplicate)
        self._editor = editor
        editor.save_requested.connect(
            lambda candidate: self._save(editor, None if duplicate else original, candidate)
        )
        try:
            editor.exec()
        finally:
            self._editor = None
            schedule_delete_later(editor)

    def _save(
        self, editor: CommandSnippetDialog, original: CommandSnippet | None, candidate: object
    ) -> None:
        if not self._active() or not self._writable or self._editor is not editor:
            return
        if not isinstance(candidate, CommandSnippet):
            editor.show_save_error(tr("Enter a name and one command line."))
            return
        if original is None:
            proposed = (*self._snippets, candidate)
        elif (
            self._find(original.snippet_id) != original
            or candidate.snippet_id != original.snippet_id
        ):
            editor.show_save_error(
                tr("This saved command changed. Close the editor and try again.")
            )
            return
        else:
            proposed = tuple(candidate if s == original else s for s in self._snippets)
        if self._commit(proposed, editor.show_save_error):
            self._dialog.set_snippets(self._snippets, select_id=candidate.snippet_id)
            editor.accept()
            self.message.emit(tr("Saved command updated."))

    def _commit(
        self, proposed: Iterable[CommandSnippet], show_error: Callable[[str], None]
    ) -> bool:
        try:
            validated = validate_command_snippets(proposed)
        except (ValueError, TypeError) as error:
            show_error(validation_text(str(error)))
            return False
        try:
            if self._store is not None:
                self._store.save(validated)
        except CommandSnippetStoreError:
            show_error(
                tr("Could not save commands. Check file permissions and free space, then retry.")
            )
            return False
        self._snippets = validated
        self.library_changed.emit(validated)
        return self._active()

    def _delete(self, snippet_id: object) -> None:
        if (
            not self._active()
            or not self._writable
            or self._confirmation is not None
            or self._favoriting
        ):
            return
        original = self._find(snippet_id)
        if original is None:
            return
        confirmation = ActionDialog(
            object_name="deleteSavedCommandDialog",
            window_title=tr("Delete saved command"),
            title=tr("Delete this saved command?"),
            subtitle=original.name,
            body=tr("Only the library entry is removed. Nothing is sent to a terminal."),
            primary_text=tr("Delete"),
            primary_object_name="confirmDeleteSavedCommandButton",
            secondary_text=tr("Keep command"),
            secondary_object_name="cancelDeleteSavedCommandButton",
            primary_role="danger",
            parent=self._dialog,
        )
        self._confirmation = confirmation
        try:
            accepted = confirmation.exec() == QDialog.DialogCode.Accepted
        finally:
            self._confirmation = None
            schedule_delete_later(confirmation)
        if not accepted or not self._active() or self._find(snippet_id) != original:
            return
        if self._commit(tuple(s for s in self._snippets if s != original), self._dialog.show_error):
            self._dialog.set_snippets(self._snippets)
            self.message.emit(tr("Saved command removed from the library."))

    def _target_available(self) -> bool:
        return self._target_id is not None and self._workspace.command_insertion_available(
            self._target_id
        )

    def _toggle_favorite(self, snippet_id: object) -> None:
        """Persist this visible entry's explicit mark before changing the library UI."""
        if (
            not self._active()
            or not self._writable
            or self._inserting
            or self._favoriting
            or self._editor is not None
            or self._parameters is not None
            or self._confirmation is not None
            or self._dialog.selected_snippet_id != snippet_id
        ):
            return
        original = self._find(snippet_id)
        if original is None:
            return
        if original not in self._dialog.snippets:
            self._dialog.show_error(
                tr("This saved command changed. Reopen the library before updating its favorite.")
            )
            return
        candidate = replace(original, favorite=not original.favorite)
        proposed = tuple(candidate if item == original else item for item in self._snippets)
        self._favoriting = True
        try:
            if self._commit(proposed, self._dialog.show_error):
                self._dialog.set_snippets(self._snippets, select_id=original.snippet_id)
                self.message.emit(
                    tr("Saved command favorite updated. Nothing was sent to a terminal.")
                )
        finally:
            self._favoriting = False

    def _insert(self, snippet_id: object) -> None:
        if not self._active() or self._inserting or self._favoriting:
            return
        snippet = self._find(snippet_id)
        if snippet is None:
            return
        dialog = self._dialog
        target_id = self._target_id
        self._inserting = True
        try:
            command = self._command_for_insertion(
                snippet,
                parent=dialog,
                target_label=self._target_label or tr("Original terminal"),
                available=lambda: self._active() and self._target_available(),
                show_error=dialog.show_error,
            )
            if command is None:
                return
            inserted = self._target_available() and self._workspace.insert_saved_command(
                target_id, command
            )
        finally:
            self._inserting = False
        if not self._active():
            return
        if not inserted:
            self._dialog.set_insertion_available(False)
            self._dialog.show_error(
                tr(
                    "The original terminal is not ready. Close this window and reopen Saved commands from the terminal you want to use."
                )
            )
            return
        self._target_id = None
        dialog.set_insertion_available(False)
        dialog.accept()
        self.message.emit(
            tr("Command inserted; Enter was sent only for line breaks in the command.")
        )
        QTimer.singleShot(0, self, lambda: self._workspace.focus_command_target(target_id))
