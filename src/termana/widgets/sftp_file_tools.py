"""Owner-pinned file operations kept separate from browser navigation/batches."""

import os
import posixpath
from pathlib import Path

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QDialog, QInputDialog
from shiboken6 import isValid

from termana.i18n import tr
from termana.models.sftp import MAX_SFTP_EDIT_BYTES, SftpOperation, SftpRequest
from termana.terminal.sftp_edit_workspace import (
    create_edit_workspace,
    read_edit_copy,
    write_edit_copy,
)
from termana.widgets.sftp_text_editor import SftpTextEditor

MAX_REMOTE_EDITORS = 4
_RECOVERY_EDITORS = set()


class _DetachedWorkspace:
    """Keep the journal until both editor decision and worker acknowledgement."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.worker_done = self.editor_done = self.preserve = False

    def cleanup(self):
        self.worker_done = True
        self._finish()

    def release(self, *, preserve=False):
        self.editor_done = True
        self.preserve |= preserve
        self._finish()

    def _finish(self):
        if self.worker_done and self.editor_done and self.workspace is not None:
            workspace, self.workspace = self.workspace, None
            if self.preserve:
                workspace.preserve()
            else:
                workspace.cleanup()


def retire_workspace(backend, request_ids, workspace):
    """A detached cleanup owner waits for worker acknowledgement, not sidebar life."""
    remaining = set(request_ids)
    if not remaining or not isValid(backend):
        workspace.cleanup()
        return
    done = [False]

    def finish(*_):
        if done[0]:
            return
        done[0] = True
        workspace.cleanup()
        if isValid(backend):
            backend.sftp_result.disconnect(result)
            backend.finished.disconnect(finish)
            backend.destroyed.disconnect(finish)

    def result(value):
        remaining.discard(value.request_id)
        if not remaining:
            finish()

    backend.sftp_result.connect(result)
    backend.finished.connect(finish)
    backend.destroyed.connect(finish)
    for identity in remaining:
        backend.cancel_sftp(identity)


class SftpFileTools(QObject):
    def __init__(self, browser):
        super().__init__(browser)
        self.browser = browser
        self._jobs = {}
        self._editors = {}
        self._downloads = {}
        self._closed = False
        browser._backend.sftp_result.connect(self._result)

    def selection(self):
        view = self.browser
        entry = view._selected_entry()
        if (
            self._closed
            or not view._directory_ready(view._directory)
            or entry is None
            or entry.kind != "file"
        ):
            return None
        return view._directory, entry

    def rename(self):
        context = self.selection()
        if context is None:
            return
        directory, entry = context
        dialog = QInputDialog(self.browser)
        dialog.setWindowTitle(tr("Rename or move remote file"))
        dialog.setLabelText(
            tr(
                "New filename here, or explicit absolute remote path. Existing targets are never replaced."
            )
        )
        dialog.setTextValue(entry.name)
        self.browser._folder_dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted and isValid(dialog)
            name = dialog.textValue() if accepted else ""
        finally:
            self.browser._folder_dialog = None
            if isValid(dialog):
                dialog.deleteLater()
        if not accepted or not isValid(self.browser) or self.selection() != context:
            return
        try:
            if not name.startswith("/") and (not name or "/" in name or name in (".", "..")):
                raise ValueError("A relative rename must be one basename")
            target = name if name.startswith("/") else posixpath.join(directory, name)
            request = SftpRequest(
                SftpOperation.RENAME, posixpath.join(directory, entry.name), target_path=target
            )
        except (ValueError, UnicodeError):
            self.browser.status.setText(
                tr("Choose a different filename or absolute path, without . or .. components.")
            )
            return
        self.browser._submit(request)

    def edit(self):
        context = self.selection()
        if context is None:
            return
        view = self.browser
        if os.name not in ("posix", "nt"):
            view.status.setText(tr("Private remote editing is unavailable on this platform."))
            return
        directory, entry = context
        remote_path = posixpath.join(directory, entry.name)
        for editor, (_workspace, original, _version) in tuple(self._editors.items()):
            if isValid(editor) and original.remote_path == remote_path:
                view.status.setText(tr("This file already has an edited copy for this SSH owner."))
                editor.show()
                return
        if any(
            request.remote_path == remote_path for _workspace, request in self._downloads.values()
        ):
            view.status.setText(tr("This file is already being opened for this SSH owner."))
            return
        if len(self._editors) + len(self._downloads) >= MAX_REMOTE_EDITORS:
            view.status.setText(tr("Close an edited copy first (4 maximum)."))
            return
        if entry.size is None or entry.size > MAX_SFTP_EDIT_BYTES:
            view.status.setText(tr("Open UTF-8 regular text files up to 2 MiB only."))
            return
        workspace = None
        try:
            workspace = create_edit_workspace()
            path = Path(workspace.name) / "copy.txt"
            edit_target = None
            if os.name == "nt":
                from termana.terminal.windows_sftp_edit_download import WindowsEditDownloadTarget

                edit_target = WindowsEditDownloadTarget(workspace, path)
            request = SftpRequest(
                SftpOperation.EDIT_DOWNLOAD, remote_path, path, edit_download_target=edit_target
            )
        except (OSError, ValueError):
            if workspace is not None:
                workspace.cleanup()
            view.status.setText(
                tr("Could not create a private local editing folder. No SSH request was sent.")
            )
            return
        if view._submit(request):
            self._downloads[request.request_id] = (workspace, request)
        else:
            workspace.cleanup()

    def _result(self, result):
        download = self._downloads.pop(result.request_id, None)
        if download is not None:
            workspace, request = download
            if (
                self._closed
                or not self.browser.available
                or result.status != "ok"
                or result.version is None
            ):
                workspace.cleanup()
                return
            try:
                data = read_edit_copy(workspace, request.local_path, result.version)
                editor = SftpTextEditor(
                    self.browser.window(),
                    title=self.browser.owner_label.text() + " · " + request.remote_path,
                    data=data,
                    checkpoint=workspace.checkpoint,
                )
            except (ValueError, OSError, UnicodeError):
                workspace.cleanup()
                self.browser.status.setText(
                    tr(
                        "Editing supports lossless UTF-8 text up to 2 MiB; this file was not opened."
                    )
                )
                return
            self._editors[editor] = (workspace, request, result.version)
            editor.save_requested.connect(lambda: self._save(editor))
            editor.closed.connect(lambda: self._release(editor))
            editor.destroyed.connect(lambda: self._release(editor, preserve=True))
            # Modeless: no nested file picker/overwrite consent and no automatic focus.
            editor.show()
            return
        editor = self._jobs.pop(result.request_id, None)
        if editor is None or editor not in self._editors or not isValid(editor):
            return
        editor.set_pending(False)
        if result.status == "ok" and result.version is not None:
            workspace, request, _ = self._editors[editor]
            self._editors[editor] = (workspace, request, result.version)
            editor.editor.document().setModified(False)
            editor.note.setText(
                tr("Saved to this SSH owner. Further saves will check this version.")
            )
        else:
            editor.note.setText(
                result.message
                or tr("Not saved. Keep a local copy before reopening the remote file.")
            )

    def _save(self, editor):
        if (
            self._closed
            or editor not in self._editors
            or not isValid(editor)
            or editor in self._jobs.values()
            or not editor.editor.document().isModified()
        ):
            return
        if not self.browser.available:
            editor.set_live(False)
            return
        _workspace, original, version = self._editors[editor]
        try:
            data = editor.encoded_text()
            # A server save can happen before the debounce timer fires. Keep
            # its local recovery bytes separately from the upload source.
            if not _workspace.checkpoint(data):
                editor.recovery_status.setText(
                    tr("Recovery checkpoint unavailable. Use Save local copy to keep this edit.")
                )
            write_edit_copy(_workspace, original.local_path, data)
            request = SftpRequest(
                SftpOperation.EDIT_UPLOAD,
                original.remote_path,
                original.local_path,
                True,
                version=version,
            )
        except (ValueError, UnicodeError, OSError):
            editor.note.setText(tr("Choose valid UTF-8 text up to 2 MiB. No SSH request was sent."))
            return
        if self.browser._submit(request):
            self._jobs[request.request_id] = editor
            editor.set_pending(True)

    def _release(self, editor, *, preserve=False):
        item = self._editors.pop(editor, None)
        if item is not None:
            pending = [identity for identity, owner in self._jobs.items() if owner is editor]
            if preserve:
                owner = _DetachedWorkspace(item[0])
                owner.release(preserve=True)
                retire_workspace(self.browser._backend, pending, owner)
            else:
                retire_workspace(self.browser._backend, pending, item[0])

    def ended(self):
        self._closed = True
        for editor in tuple(self._editors):
            if isValid(editor):
                editor.set_pending(False)
                editor.set_live(False)

    def dispose(self):
        self.ended()
        for editor in tuple(self._editors):
            if isValid(editor):
                if editor.editor.document().isModified():
                    # Closing an SSH tab must not silently discard dirty text.
                    # Detach this inert local copy; no backend or saved remote
                    # capability survives. Workspace cleanup waits for its job.
                    editor.save_requested.disconnect()
                    editor.closed.disconnect()
                    editor.destroyed.disconnect()
                    workspace, _request, _version = self._editors.pop(editor)
                    pending = [
                        identity for identity, owner in self._jobs.items() if owner is editor
                    ]
                    retired = _DetachedWorkspace(workspace)
                    retire_workspace(self.browser._backend, pending, retired)
                    # A reviewed close discards. Unexpected QObject destruction
                    # preserves the checkpoint instead of pretending consent.
                    reviewed = [False]

                    def closed(retired=retired, reviewed=reviewed):
                        reviewed[0] = True
                        retired.release()

                    editor.closed.connect(closed)
                    editor.destroyed.connect(
                        lambda _=None, retired=retired, reviewed=reviewed: retired.release(
                            preserve=not reviewed[0]
                        )
                    )
                    editor.setParent(None, Qt.WindowType.Dialog)
                    _RECOVERY_EDITORS.add(editor)
                    editor.destroyed.connect(
                        lambda _=None, editor=editor: _RECOVERY_EDITORS.discard(editor)
                    )
                    editor.show()
                else:
                    self._release(editor)
                    editor.close()
            else:
                self._release(editor, preserve=True)
        for identity, (workspace, _request) in tuple(self._downloads.items()):
            retire_workspace(self.browser._backend, (identity,), workspace)
        self._downloads.clear()
