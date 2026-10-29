"""Explicit, session-local quick folders on the browser's original SSH owner."""

import unicodedata

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QComboBox, QSizePolicy, QVBoxLayout, QWidget
from shiboken6 import isValid

from termana.i18n import tr
from termana.models.sftp_places import SessionSftpPlaces, validate_sftp_place
from termana.ui_text import plain_text_tooltip
from termana.widgets.adaptive_button_row import AdaptiveButtonRow
from termana.widgets.wrapping_button import WrappingPushButton


def place_label(path):
    """Keep control/format/separator characters visible as inert escape text."""
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char
        for char in path
    )


class SftpPlaces(QWidget):
    def __init__(self, browser):
        super().__init__(browser)
        self.browser = browser
        self._owner = browser._backend
        self._closed = False
        self._directory = None
        self.places = SessionSftpPlaces()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.selector = QComboBox()
        self.selector.setAccessibleName(tr("Quick remote folders for this SSH session"))
        self.selector.setPlaceholderText(tr("Quick folders · this SSH session only"))
        self.selector.setMinimumWidth(0)
        self.selector.setMinimumContentsLength(1)
        self.selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.selector.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.selector)
        self.pin_button = WrappingPushButton(tr("Pin current"))
        self.go_button = WrappingPushButton(tr("Go"))
        self.unpin_button = WrappingPushButton(tr("Unpin"))
        self.clear_button = WrappingPushButton(tr("Clear pins"))
        for button, label, callback in (
            (
                self.pin_button,
                tr("Pin current remote folder for this SSH session"),
                self.pin_current,
            ),
            (self.go_button, tr("Go to selected quick remote folder"), self.go),
            (self.unpin_button, tr("Unpin selected quick remote folder"), self.unpin),
            (self.clear_button, tr("Clear this SSH session's quick folders"), self.clear),
        ):
            button.setAccessibleName(label)
            button.setAutoDefault(False)
            button.clicked.connect(callback)
        layout.addWidget(
            AdaptiveButtonRow(
                (self.pin_button, self.go_button, self.unpin_button, self.clear_button)
            )
        )
        self.setToolTip(
            tr(
                "Pins stay only in this SSH session. Selecting a path does not navigate; use Go. Pin, Unpin and Clear pins do not send SSH requests or remove remote files."
            )
        )
        self.selector.currentIndexChanged.connect(self.update_state)
        self.update_state()

    def _live(self):
        return (
            isValid(self)
            and not self._closed
            and isValid(self.browser)
            and self.browser._backend is self._owner
            and isValid(self._owner)
            and self.browser.available
        )

    def _ready(self):
        return self._live() and self.browser._directory_ready(self._directory)

    def _selected_path(self):
        path = self.selector.currentData()
        return path if type(path) is str and path in self.places.snapshot() else None

    def directory_changed(self, path):
        if not self._live():
            return
        try:
            validate_sftp_place(path)
        except (ValueError, UnicodeError):
            self._directory = None
        else:
            self._directory = path
        self.update_state()

    def update_state(self, *_):
        if not isValid(self):
            return
        live = self._live()
        ready = self._ready()
        selected = self._selected_path()
        has_pins = bool(self.places.snapshot())
        focus = self.focusWidget()
        folding_focus = focus in (
            self.selector,
            self.go_button,
            self.unpin_button,
            self.clear_button,
        )
        self.pin_button.setEnabled(ready)
        self.selector.setVisible(has_pins)
        self.selector.setEnabled(live and has_pins)
        self.go_button.setEnabled(ready and selected is not None)
        self.unpin_button.setEnabled(live and selected is not None)
        self.clear_button.setEnabled(live and has_pins)
        for button in (self.go_button, self.unpin_button, self.clear_button):
            button.setVisible(has_pins)
        if not has_pins and folding_focus and self.isVisible():
            target = self.pin_button if ready else self.browser.path_input
            target.setFocus(Qt.FocusReason.OtherFocusReason)
        self.selector.setToolTip(plain_text_tooltip(place_label(selected)) if selected else "")

    def _refresh(self, selected=None):
        with QSignalBlocker(self.selector):
            self.selector.clear()
            for path in self.places.snapshot():
                self.selector.addItem(place_label(path), path)
            self.selector.setCurrentIndex(self.selector.findData(selected) if selected else -1)
        self.update_state()

    def pin_current(self):
        if not self._ready():
            return False
        try:
            added = self.places.pin(self._directory)
        except (ValueError, UnicodeError):
            self.browser.status.setText(
                tr(
                    "Quick folders need a canonical path, with at most 20 paths / 128 KiB per SSH session."
                )
            )
            return False
        self._refresh(self._directory)
        self.browser.status.setText(
            tr("Folder pinned for this SSH session only. No SSH request was sent.")
            if added
            else tr("This folder is already pinned for this SSH session.")
        )
        return added

    def go(self):
        if not self._ready():
            return False
        path = self._selected_path()
        if path is None:
            return False
        return self.browser.navigate(path)

    def unpin(self):
        if not self._live():
            return False
        path = self._selected_path()
        if path is None or not self.places.unpin(path):
            return False
        self._refresh()
        self.browser.status.setText(
            tr("Quick folder unpinned locally. No remote file was changed.")
        )
        return True

    def clear(self):
        if not self._live():
            return
        self.places.clear()
        self._refresh()
        self.browser.status.setText(
            tr("Quick folders cleared locally. No remote file was changed.")
        )

    def dispose(self):
        self._closed = True
        self._directory = None
        self.places.clear()
        if isValid(self):
            self._refresh()
