"""Like-for-like metadata checks for bounded SSH security-file snapshots."""

import os
from typing import Final

SSH_FILE_HANDLE_FIELDS: Final = (
    "st_dev",
    "st_ino",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
    "st_mode",
    "st_uid",
)
SSH_FILE_PATH_FIELDS: Final = tuple(
    name for name in SSH_FILE_HANDLE_FIELDS if os.name != "nt" or name != "st_ctime_ns"
)


def same_ssh_path_status(handle, path, fields=SSH_FILE_PATH_FIELDS) -> bool:
    # Windows path ctime is deprecated creation-time metadata while handle
    # ctime can be change time. Compare birthtime across APIs instead.
    return all(getattr(handle, name) == getattr(path, name) for name in fields) and getattr(
        handle, "st_birthtime_ns", None
    ) == getattr(path, "st_birthtime_ns", None)


def same_ssh_handle_status(before, after) -> bool:
    return all(getattr(before, name) == getattr(after, name) for name in SSH_FILE_HANDLE_FIELDS)
