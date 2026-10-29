"""Bounded in-memory quick folders for one SFTP browser, never persistent."""

from termana.models.sftp import MAX_SFTP_PATH_CHARACTERS

MAX_SFTP_PLACES = 20
MAX_SFTP_PLACE_BYTES = 128 * 1024


def validate_sftp_place(path):
    """Validate a server-canonical absolute identity without normalizing it."""
    if (
        type(path) is not str
        or not path.startswith("/")
        or len(path) > MAX_SFTP_PATH_CHARACTERS
        or "\0" in path
        or (path != "/" and any(part in ("", ".", "..") for part in path.split("/")[1:]))
    ):
        raise ValueError("Quick folders require a canonical absolute remote directory.")
    return len(path.encode("utf-8"))


class SessionSftpPlaces:
    def __init__(self):
        self._paths = []
        self._bytes = 0

    def snapshot(self):
        return tuple(self._paths)

    def pin(self, path):
        size = validate_sftp_place(path)
        if path in self._paths:
            return False
        if len(self._paths) >= MAX_SFTP_PLACES or self._bytes + size > MAX_SFTP_PLACE_BYTES:
            raise ValueError("Quick folders are limited to 20 paths and 128 KiB per SSH session.")
        self._paths.append(path)
        self._bytes += size
        return True

    def unpin(self, path):
        if type(path) is not str or path not in self._paths:
            return False
        self._paths.remove(path)
        self._bytes -= len(path.encode("utf-8"))
        return True

    def clear(self):
        self._paths.clear()
        self._bytes = 0
