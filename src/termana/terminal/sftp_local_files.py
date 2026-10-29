"""Pin one selected regular upload source without following a changed leaf."""

import os
import stat
from contextlib import contextmanager

from termana.credentials.ssh_file_metadata import same_ssh_handle_status, same_ssh_path_status


class SftpSourceChangedError(ValueError):
    def __init__(self):
        super().__init__("The local source changed or is not a supported regular file.")


@contextmanager
def open_sftp_source(path):
    """Read only after path/handle identity matches; recheck before remote commit.

    POSIX nonblocking/no-follow flags also prevent a replaced FIFO from blocking
    the worker. On platforms without those flags, the opened handle still must
    match the selected regular-file metadata before any source bytes are read.
    This is a consistency check, not a filesystem snapshot or parent-path sandbox.
    """
    selected = path.lstat()
    if not stat.S_ISREG(selected.st_mode):
        raise SftpSourceChangedError()
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY", "O_NOINHERIT"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not same_ssh_path_status(before, selected):
            raise SftpSourceChangedError()
        source = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with source:
        yield source
        # Skip this check when the body already raised: do not replace its
        # cancellation/remote failure with an unrelated local-source message.
        try:
            unchanged = same_ssh_handle_status(
                before, os.fstat(source.fileno())
            ) and same_ssh_path_status(before, path.lstat())
        except OSError:
            unchanged = False
        if not unchanged:
            raise SftpSourceChangedError()
