"""Bounded transfer plans, not mutable global overwrite permission."""

import os
import posixpath
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from termana.models.sftp import SftpOperation, SftpRequest

MAX_SFTP_BATCH_FILES = 64
MAX_SFTP_BATCH_CONTEXTS = 16
_WINDOWS_DEVICE = re.compile(
    r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I
)


@dataclass
class SftpBatchContext:
    directory: str
    batch_id: UUID = field(default_factory=uuid4)
    policy: str = "ask"
    pending: set[UUID] = field(default_factory=set)


def local_download_name(name):
    if (
        not name
        or name in (".", "..")
        or any(c in name for c in '\\/:<>"|?*\0')
        or name.endswith((".", " "))
        or _WINDOWS_DEVICE.match(name)
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError("Choose a portable local filename for this download.")
    return name


def upload_batch(paths, directory):
    if not 1 <= len(paths) <= MAX_SFTP_BATCH_FILES:
        raise ValueError("Select between 1 and 64 regular files per batch.")
    requests, destinations = [], set()
    for selected in paths:
        path = Path(selected)
        if not path.is_absolute() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Upload batches accept regular files, not folders or links.")
        destination = posixpath.join(directory, path.name)
        if destination in destinations:
            raise ValueError("Two selected files have the same remote destination name.")
        destinations.add(destination)
        requests.append(SftpRequest(SftpOperation.UPLOAD, destination, path))
    return tuple(requests)


def download_batch(entries, directory, target):
    if not 1 <= len(entries) <= MAX_SFTP_BATCH_FILES:
        raise ValueError("Select between 1 and 64 regular files per batch.")
    target = Path(target)
    if not target.is_absolute() or not target.is_dir():
        raise ValueError("Choose an existing local download directory.")
    requests, destinations = [], set()
    for entry in entries:
        if entry.kind != "file":
            raise ValueError("Download batches accept only regular files.")
        name = local_download_name(entry.name)
        identity = (
            unicodedata.normalize("NFC", name).casefold()
            if sys.platform == "darwin"
            else name.casefold()
            if os.name == "nt"
            else name
        )
        if identity in destinations:
            raise ValueError("Selected names can collide on the local filesystem.")
        destinations.add(identity)
        requests.append(
            SftpRequest(
                SftpOperation.DOWNLOAD, posixpath.join(directory, entry.name), target / name
            )
        )
    return tuple(requests)


def upload_selection(paths, directory):
    """File/folder roots only; expansion belongs to the worker, never the GUI."""
    if not 1 <= len(paths) <= MAX_SFTP_BATCH_FILES:
        raise ValueError("Select between 1 and 64 files or folders.")
    requests, names = [], set()
    for selected in paths:
        path = Path(selected)
        if not path.is_absolute():
            raise ValueError("Choose an absolute source path")
        value = path.lstat()
        if (
            stat.S_ISLNK(value.st_mode)
            or getattr(value, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode))
        ):
            raise ValueError("Choose regular files/folders, not links or devices")
        name = local_download_name(path.name)
        identity = unicodedata.normalize("NFC", name).casefold()
        if identity in names:
            raise ValueError("Source names collide")
        names.add(identity)
        operation = (
            SftpOperation.UPLOAD_TREE if stat.S_ISDIR(value.st_mode) else SftpOperation.UPLOAD
        )
        requests.append(SftpRequest(operation, posixpath.join(directory, name), path))
    return tuple(requests)


def download_selection(entries, directory, target):
    if not 1 <= len(entries) <= MAX_SFTP_BATCH_FILES:
        raise ValueError("Select between 1 and 64 files or folders.")
    target = Path(target)
    if not target.is_absolute() or not target.is_dir():
        raise ValueError("Choose an existing local parent folder")
    requests, names = [], set()
    for entry in entries:
        if entry.kind not in ("file", "directory"):
            raise ValueError("Choose regular files/folders, not links or devices")
        name = local_download_name(entry.name)
        identity = unicodedata.normalize("NFC", name).casefold()
        if identity in names:
            raise ValueError("Destination names collide")
        names.add(identity)
        operation = (
            SftpOperation.DOWNLOAD_TREE if entry.kind == "directory" else SftpOperation.DOWNLOAD
        )
        requests.append(
            SftpRequest(operation, posixpath.join(directory, entry.name), target / name)
        )
    return tuple(requests)
