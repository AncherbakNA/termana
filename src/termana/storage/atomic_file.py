"""Shared same-directory atomic replacement for validated application snapshots."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def _publish_stage(temporary: Path, destination: Path) -> Path:
    if os.name != "nt":
        return temporary.replace(destination)
    # Reopening a DELETE-held application-data parent during MoveFileEx fails
    # with sharing violation. Reuse the reviewed same-directory NULL-root rename
    # on this exact stage handle instead of weakening the directory's sharing.
    from termana.credentials.windows_private_files import _open, _rename, _write_api
    from termana.credentials.windows_ssh_files import WindowsSshFileError

    handle = None
    try:
        if temporary.parent != destination.parent:
            raise OSError("Atomic replacement must stay in the same directory")
        handle = _open(temporary, writable=True)
        _rename(handle, None, destination.name, replace=True)
        return destination
    except WindowsSshFileError:
        raise OSError("Native atomic snapshot publication failed") from None
    finally:
        if handle is not None:
            _write_api().CloseHandle(handle)


def replace_file_atomically(path: Path, content: bytes) -> None:
    """Publish complete bytes only after flushing and closing a private temporary.

    Serialization, size limits, write guards, and user-facing errors belong to
    each store. This helper preserves the existing destination on write failure
    and supports Windows, where an open temporary cannot be replaced safely.
    """
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        _publish_stage(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # Cleanup must not replace the original storage error.
                pass
