"""Shared same-directory atomic replacement for validated application snapshots."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


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

        temporary_path.replace(path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # Cleanup must not replace the original storage error.
                pass
