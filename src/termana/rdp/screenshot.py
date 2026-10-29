"""User-requested PNG export: owner-only new regular file, never overwrite."""

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice


def save_screenshot(image, target):
    target = Path(target)
    if (image.isNull() or image.width() > 1920 or image.height() > 1080
            or not target.is_absolute()):
        raise ValueError("Invalid desktop screenshot or destination")
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG") or buffer.size() > 16 * 1024 * 1024:
        raise ValueError("Could not encode the desktop screenshot")
    data = bytes(buffer.data())
    descriptor, temporary = tempfile.mkstemp(prefix=".termana-snapshot-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        # Publish the complete new inode atomically; EEXIST preserves existing
        # regular files/symlinks. No truncation or replace of a previous export.
        os.link(temporary, target, follow_symlinks=False)
    finally:
        os.unlink(temporary)
