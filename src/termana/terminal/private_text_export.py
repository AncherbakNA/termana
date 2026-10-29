"""Export one bounded local copy, never replacing a chosen existing file."""

import os
import tempfile
from pathlib import Path

from termana.models.sftp import MAX_SFTP_EDIT_BYTES


def export_private_text_copy(target: Path, data: bytes) -> None:
    if not isinstance(target, Path) or not target.is_absolute():
        raise ValueError("Choose an absolute local path")
    if type(data) is not bytes or len(data) > MAX_SFTP_EDIT_BYTES:
        raise ValueError("Invalid local edited copy")
    if os.name == "nt":
        from termana.credentials.windows_private_files import write_windows_private_export
        from termana.credentials.windows_ssh_files import WindowsSshFileError

        try:
            write_windows_private_export(target, data)
        except WindowsSshFileError:
            raise ValueError("Local export could not be confirmed safely") from None
        return
    if os.name != "posix":
        raise ValueError("Private local export is unavailable on this platform")
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".termana-edit-export-", suffix=".part", dir=target.parent, delete=False
        ) as stream:
            staged = Path(stream.name)
            stream.write(data)
        os.link(staged, target)  # Exclusive publication; never replace a target.
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
