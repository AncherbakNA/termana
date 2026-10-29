"""Private, consent-owned creation of one SSH parent directory, not a repair tool."""

import os
import stat
from pathlib import Path

from termana.credentials.embedded_ssh_trust import EmbeddedTrustFileError


def safe_owned_directory(path: Path) -> bool:
    try:
        selected = path.lstat()
        return (
            stat.S_ISDIR(selected.st_mode)
            and selected.st_uid == os.getuid()
            and not stat.S_IMODE(selected.st_mode) & 0o022
        )
    except OSError:
        return False


def can_create_parent(path: Path) -> bool:
    if os.name != "posix" or not safe_owned_directory(path.parent.parent):
        return False
    try:
        path.parent.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def create_parent_after_consent(path: Path) -> None:
    if not can_create_parent(path):
        raise EmbeddedTrustFileError()
    parent = path.parent.parent
    before = parent.lstat()
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(directory)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EmbeddedTrustFileError()
        # EEXIST is an external creation, not permission to use another object.
        os.mkdir(path.parent.name, 0o700, dir_fd=directory)
        after = parent.lstat()
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise EmbeddedTrustFileError()
        if not safe_owned_directory(path.parent):
            raise EmbeddedTrustFileError()
    finally:
        os.close(directory)
