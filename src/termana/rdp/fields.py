"""Readable identity fields which let QFormLayout wrap labels at large fonts."""
from PySide6.QtWidgets import QLineEdit

from termana.theme import FORM_FIELD_HORIZONTAL_PADDING


class RdpIdentityLineEdit(QLineEdit):
    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        # The native minimum permits a 3-4 character field beside a long label.
        # Reserve one ordinary Windows username; WrapLongRows can then move the
        # label above the field instead of squeezing it or widening the dialog.
        size.setWidth(max(size.width(), self.fontMetrics().horizontalAdvance("administrator")
                          + 2*FORM_FIELD_HORIZONTAL_PADDING+4))
        return size
