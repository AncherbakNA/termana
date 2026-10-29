"""Explicit local-only recovery review. Never displays recovered text or paths."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)
from shiboken6 import isValid

from termana.i18n import tr
from termana.terminal.private_recovery import RecoveryExportUnavailable, RecoverySession
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.wrapping_label import WrappingLabel


class PrivateRecoveryDialog(QDialog):
    def __init__(self, parent=None, *, root=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Private local recovery"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(620, 360)
        self._session = None
        self._closed, self._closing, self._revision = False, False, 0
        self._picker = self._review = None
        layout = QVBoxLayout(self)
        explanation = WrappingLabel(
            tr(
                "Only registered private copies from inactive owners are listed. Save a new local copy, or explicitly discard a selected record. Nothing reconnects, uploads, or changes host trust. Recent typing and unknown or older unregistered leftovers may not be recoverable."
            )
        )
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(explanation)
        self.items = QListWidget()
        self.items.setAccessibleName(tr("Private recovery records without sensitive metadata"))
        self.items.currentItemChanged.connect(self._selection_changed)
        self.items.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.items)
        self.status = WrappingLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.export_button = QPushButton(tr("Save local copy…"))
        self.discard_button = QPushButton(tr("Discard selected…"))
        self.close_button = QPushButton(tr("Close"))
        for button in (self.export_button, self.discard_button, self.close_button):
            button.setAutoDefault(False)
        self.export_button.clicked.connect(self._export)
        self.discard_button.clicked.connect(self._discard)
        self.close_button.clicked.connect(self.close)
        layout.addWidget(
            AdaptiveButtonRow((self.export_button, self.discard_button, self.close_button))
        )
        try:
            self._session = RecoverySession(root)
            self._populate()
        except Exception:
            self.status.setText(
                tr("Private recovery is unavailable. Existing copies were preserved.")
            )
        self._selection_changed()
        # Capture the non-QObject session, not a bound slot on the dying dialog.
        session = self._session
        self.destroyed.connect(lambda *_: session.close() if session is not None else None)

    def _populate(self):
        self.items.clear()
        for index, offer in enumerate(self._session.offers, 1):
            label = (
                tr("Local edited copy {p0} · {p1} bytes", p0=f"{index}", p1=f"{offer.size:,}")
                if offer.kind == "editor"
                else tr(
                    "Private transaction receipt {p0} · original temporary file absent",
                    p0=f"{index}",
                )
                if offer.kind == "receipt"
                else tr("Unpublished private temporary file {p0}", p0=f"{index}")
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, offer)
            self.items.addItem(item)
        self.status.setText(
            tr("Select a record. Export never replaces an existing local file.")
            if self._session.offers
            else tr("No safely recoverable inactive records were found.")
        )

    def _selected(self):
        item = self.items.currentItem()
        if item is None or not item.isSelected() or self.items.selectedItems() != [item]:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _selection_changed(self, *_):
        self._revision += 1
        offer = self._selected()
        self.export_button.setEnabled(
            not self._closed and offer is not None and offer.kind == "editor"
        )
        self.discard_button.setEnabled(not self._closed and offer is not None)

    def _current(self, session, revision, identity):
        return (
            isValid(self)
            and not self._closed
            and self._session is session
            and self._revision == revision
            and self._selected() is not None
            and self._selected().identity == identity
        )

    def _export(self):
        offer = self._selected()
        if (
            self._closed
            or offer is None
            or offer.kind != "editor"
            or self._picker is not None
            or self._review is not None
        ):
            return
        session, revision = self._session, self._revision
        picker = QFileDialog(self, tr("Save recovered local copy"))
        picker.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        picker.setFileMode(QFileDialog.FileMode.AnyFile)
        picker.selectFile("recovered-edit.txt")
        self._picker = picker
        try:
            accepted = picker.exec() == QDialog.DialogCode.Accepted and isValid(picker)
            paths = picker.selectedFiles() if accepted else []
        finally:
            self._picker = None
            if isValid(picker):
                picker.deleteLater()
        if not paths or not self._current(session, revision, offer.identity):
            return
        try:
            session.export(offer.identity, Path(paths[0]))
            self.status.setText(
                tr("New private local copy saved. The recovery record remains available.")
            )
        except RecoveryExportUnavailable:
            self.status.setText(
                tr(
                    "Recovery export is unavailable or another export is pending. Existing copies were preserved. Review any temporary recovery record explicitly, or try again after the current export ends."
                )
            )
        except Exception:
            self.status.setText(tr("Export could not be confirmed. Choose a new local filename."))

    def _discard(self):
        offer = self._selected()
        if self._closed or offer is None or self._review is not None or self._picker is not None:
            return
        session, revision = self._session, self._revision
        review = QMessageBox(self)
        review.setWindowTitle(tr("Discard private recovery?"))
        review.setText(tr("Discard this selected private record? This cannot be undone."))
        review.setStandardButtons(
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel
        )
        review.setDefaultButton(QMessageBox.StandardButton.Cancel)
        self._review = review
        try:
            accepted = review.exec() == QMessageBox.StandardButton.Discard
        finally:
            self._review = None
            if isValid(review):
                review.deleteLater()
        if not accepted or not self._current(session, revision, offer.identity):
            return
        try:
            session.discard(offer.identity)
            self._populate()
        except Exception:
            self.status.setText(
                tr("Discard could not be confirmed. Uncertain objects were preserved.")
            )

    def _release_session(self, *_):
        if self._session is not None:
            self._session.close()
            self._session = None

    def closeEvent(self, event):
        self._closed = True
        self._revision += 1
        for dialog in (self._picker, self._review):
            if dialog is not None and isValid(dialog):
                dialog.reject()
        self._release_session()
        self._closing = True
        try:
            super().closeEvent(event)
        finally:
            self._closing = False

    def reject(self):
        if self._closing:
            super().reject()
        else:
            self.close()
