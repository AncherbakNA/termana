"""Folder pickers and conflict consent bound to one browser, owner and selection."""

import posixpath
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox
from shiboken6 import isValid

from termana.i18n import tr
from termana.models.sftp import MAX_SFTP_PENDING, SftpOperation, SftpTreePolicy
from termana.models.sftp_batch import download_selection, local_download_name, upload_selection
from termana.widgets.sftp_file_dialog import choose_sftp_directory
from termana.widgets.themed_checkbox import ThemedCheckBox


class SftpFolderTransfers:
    def __init__(self, browser):
        self.browser = browser

    def _context(self):
        view = self.browser
        if not isValid(view) or not view.available:
            return None
        return view._directory, view._listing_generation, view._selected_entries()

    def _current(self, context):
        view = self.browser
        valid = (
            context is not None
            and isValid(view)
            and view._directory_ready(context[0])
            and self._context() == context
        )
        if not valid:
            view._modal_context_changed()
        return valid

    def _confirm(self, requests, context):
        from termana.widgets.sftp_browser import file_label

        view = self.browser
        if not self._current(context):
            return False
        message = QMessageBox(view)
        message.setWindowTitle(tr("Copy folders and files?"))
        message.setTextFormat(Qt.TextFormat.PlainText)
        names = "\n".join(file_label(posixpath.basename(r.remote_path))[:120] for r in requests[:8])
        downloading = requests[0].operation in (SftpOperation.DOWNLOAD, SftpOperation.DOWNLOAD_TREE)
        destination = str(requests[0].local_path.parent) if downloading else context[0]
        message.setText(
            tr(
                "SSH owner: {p0}\n{p1}:\n{p2}\n\n{p3} selected items\n{p4}\n\nInclude subfolders and empty folders. Links, junctions and special entries are excluded, never followed. Existing file conflicts require confirmation. Cancel keeps completed files; it does not roll back the folder. Originals are never moved or deleted. The queue admits at most {p5} jobs; any unqueued items are shown and are not sent.",
                p0=f"{view.owner_label.text()}",
                p1=f"{('Download to local folder' if downloading else 'Upload to remote folder')}",
                p2=f"{file_label(destination)[:512]}",
                p3=f"{len(requests)}",
                p4=f"{names}",
                p5=f"{MAX_SFTP_PENDING}",
            )
        )
        message.setDetailedText(
            "\n\n".join(
                tr(
                    "Source: {p0}\nDestination: {p1}",
                    p0=f"{file_label(r.remote_path if downloading else str(r.local_path))}",
                    p1=f"{file_label(str(r.local_path) if downloading else r.remote_path)}",
                )
                for r in requests
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        message.setDefaultButton(QMessageBox.StandardButton.Cancel)
        view._overwrite_dialog = message
        try:
            accepted = message.exec() == QMessageBox.StandardButton.Yes
        finally:
            view._overwrite_dialog = None
            if isValid(message):
                message.deleteLater()
        return accepted and self._current(context)

    def upload(self):
        view = self.browser
        context = self._context()
        if context is None or not view._directory_ready(context[0]):
            return
        selected = choose_sftp_directory(view, tr("Upload folder including subfolders"))
        if not selected or not self._current(context):
            return
        try:
            requests = upload_selection((Path(selected).absolute(),), context[0])
        except (OSError, ValueError):
            view.status.setText(tr("Choose a regular folder with a portable name, not a link."))
            return
        if self._confirm(requests, context):
            view._submit_batch(requests, context[0])

    def download(self, entries):
        view = self.browser
        context = self._context()
        if context is None or not view._directory_ready(context[0]):
            return
        target = choose_sftp_directory(view, tr("Download folders/files into this local folder"))
        if not target or not self._current(context):
            return
        try:
            requests = download_selection(entries, context[0], Path(target).absolute())
        except (OSError, ValueError):
            view.status.setText(
                tr(
                    "Choose regular files/folders with portable, distinct names and a local destination."
                )
            )
            return
        if self._confirm(requests, context):
            view._submit_batch(requests, context[0])

    def review_conflict(self, request, batch):
        from termana.widgets.sftp_browser import file_label

        view = self.browser
        context = self._context()
        if context is None or not view._directory_ready(context[0]):
            return
        message = QMessageBox(view)
        message.setWindowTitle(tr("Files already exist in this folder"))
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setText(
            tr(
                "SSH owner: {p0}\nFolder: {p1}\n\nThe scan found existing regular files. Merge subfolders and keep or replace those files? Unrelated destination entries are never deleted. Nothing has been copied by this folder job yet.",
                p0=f"{view.owner_label.text()}",
                p1=f"{file_label(request.remote_path)}",
            )
        )
        message.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel
        )
        message.button(QMessageBox.StandardButton.Yes).setText(tr("Merge and replace files"))
        message.button(QMessageBox.StandardButton.No).setText(tr("Keep existing files"))
        message.setDefaultButton(QMessageBox.StandardButton.Cancel)
        rename = message.addButton(tr("Choose another name…"), QMessageBox.ButtonRole.ActionRole)
        checkbox = None
        if batch is not None:
            checkbox = ThemedCheckBox(tr("Use this decision for remaining conflicts in this batch"))
            message.setCheckBox(checkbox)
        view._overwrite_dialog = message
        try:
            choice = message.exec()
            renamed = isValid(message) and message.clickedButton() is rename
            applies = checkbox is not None and isValid(checkbox) and checkbox.isChecked()
        finally:
            view._overwrite_dialog = None
            if isValid(message):
                message.deleteLater()
        if not self._current(context):
            return
        if renamed:
            dialog = QInputDialog(view)
            dialog.setWindowTitle(tr("New destination folder name"))
            dialog.setLabelText(tr("Folder name only, not a path:"))
            view._folder_dialog = dialog
            try:
                accepted = dialog.exec() == QInputDialog.DialogCode.Accepted
                name = dialog.textValue() if isValid(dialog) else ""
            finally:
                view._folder_dialog = None
                if isValid(dialog):
                    dialog.deleteLater()
            if accepted and self._current(context):
                try:
                    view._retry_transfer(request, batch, renamed=local_download_name(name))
                except (ValueError, OSError):
                    view.status.setText(tr("Choose a portable folder name, not a path."))
            return
        if choice not in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No):
            view.status.setText(tr("Folder transfer cancelled before copying; no files replaced."))
            view._publish_transfer(
                replace(
                    view._transfers[request.request_id],
                    status=tr("Cancelled · no folder files copied"),
                    pending=False,
                    result_status="cancelled",
                )
            )
            return
        replace_files = choice == QMessageBox.StandardButton.Yes
        if applies and batch is not None:
            batch.policy = "replace" if replace_files else "skip"
        view._retry_transfer(
            request,
            batch,
            tree_policy=SftpTreePolicy.REPLACE if replace_files else SftpTreePolicy.SKIP,
        )
